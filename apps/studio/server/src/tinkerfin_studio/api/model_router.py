"""前端可用模型目录路由"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import Field

from tinkerfin_studio.api.dependencies import (
    ModelServiceDep,
    RawTokenDep,
    UserContextDep,
    get_auth_service,
    get_auth_session,
)
from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority
from tinkerfin_studio.models.catalog import PROVIDER_PRESETS
from tinkerfin_studio.models.discovery import discover_models
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelSave,
    AgentModelSettings,
    ModelConnectionSave,
    ModelConnectionSettings,
    ModelDiscoveryResult,
    ModelSettingsOverview,
    ModelTestRequest,
    ModelTestResult,
    ProviderPreset,
)
from tinkerfin_studio.models.service import AgentModelService, connection_provider
from tinkerfin_studio.models.testing import _failure_code, run_model_test
from tinkerfin_studio.models.transport import ModelTransport
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/models", tags=["模型"])


@router.get("", response_model=ApiResponse[AgentModelCatalog], summary="模型目录")
async def list_models(
    user: UserContextDep,
    service: ModelServiceDep,
) -> ApiResponse[AgentModelCatalog]:
    """返回当前允许创建新 run 的安全模型目录"""

    del user
    return ApiResponse.success(await service.list_catalog())


@router.get("/settings", response_model=ApiResponse[ModelSettingsOverview])
async def model_settings_overview(
    service: ModelServiceDep,
) -> ApiResponse[ModelSettingsOverview]:
    """一次读取本人模型设置，避免分散加载产生重复错误提示"""
    return ApiResponse.success(
        ModelSettingsOverview(
            models=await service.settings(),
            connections=await service.connections(),
            providers=list(PROVIDER_PRESETS),
        )
    )


@router.get("/configurations", response_model=ApiResponse[list[AgentModelSettings]])
async def model_settings(
    service: ModelServiceDep,
) -> ApiResponse[list[AgentModelSettings]]:
    """返回当前用户的可编辑模型配置，密钥不回显"""
    return ApiResponse.success(await service.settings())


@router.post("/configurations", response_model=ApiResponse[None])
async def add_models(
    payload: Annotated[list[AgentModelSave], Field(min_length=1, max_length=200)],
    service: ModelServiceDep,
) -> ApiResponse[None]:
    """原子保存当前用户选择的模型"""
    await service.save_models(payload)
    return ApiResponse.success()


@router.put("/configurations/{model_id}", response_model=ApiResponse[None])
async def save_model_settings(
    model_id: str, payload: AgentModelSave, service: ModelServiceDep
) -> ApiResponse[None]:
    """保存当前用户的模型或生图服务，不能访问其他用户的同名配置"""
    if payload.model_id != model_id:
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION, message="模型标识与请求路径不一致"
        )
    await service.save_settings(payload)
    return ApiResponse.success()


@router.put("/configurations/{model_id}/default", response_model=ApiResponse[None])
async def set_default_model(
    model_id: str, service: ModelServiceDep
) -> ApiResponse[None]:
    """启用本人已保存模型并设为同用途默认项，保持连接配置不变"""
    await service.set_default(model_id)
    return ApiResponse.success()


@router.delete("/configurations/{model_id}", response_model=ApiResponse[None])
async def delete_model_settings(
    model_id: str, service: ModelServiceDep
) -> ApiResponse[None]:
    """删除本人模型配置，保留会话历史"""
    await service.delete_settings(model_id)
    return ApiResponse.success()


@router.get("/providers", response_model=ApiResponse[list[ProviderPreset]])
async def provider_presets(user: UserContextDep) -> ApiResponse[list[ProviderPreset]]:
    """返回连接默认值，不请求供应商或读取密钥"""
    del user
    return ApiResponse.success(list(PROVIDER_PRESETS))


@router.get("/connections", response_model=ApiResponse[list[ModelConnectionSettings]])
async def connections(
    service: ModelServiceDep,
) -> ApiResponse[list[ModelConnectionSettings]]:
    return ApiResponse.success(await service.connections())


@router.put("/connections/{connection_id}", response_model=ApiResponse[None])
async def save_connection(
    connection_id: str, payload: ModelConnectionSave, service: ModelServiceDep
) -> ApiResponse[None]:
    if connection_id != payload.connection_id:
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION, message="连接标识与请求路径不一致"
        )
    await service.save_connection(payload)
    return ApiResponse.success()


@router.delete("/connections/{connection_id}", response_model=ApiResponse[None])
async def delete_connection(
    connection_id: str, service: ModelServiceDep
) -> ApiResponse[None]:
    await service.delete_connection(connection_id)
    return ApiResponse.success()


async def _test_user(request: Request, token: RawTokenDep) -> UserContext:
    """测试前完成认证并归还连接，外部模型等待不占用认证数据库会话"""
    async with get_resources(request.app).database.session() as session:
        service = await get_auth_service(request, session)
        return (await get_auth_session(token, service)).user


@router.post("/configurations/test", response_model=ApiResponse[ModelTestResult])
async def test_model_configuration(
    request: Request,
    payload: ModelTestRequest,
    user: Annotated[UserContext, Depends(_test_user)],
) -> ApiResponse[ModelTestResult]:
    """仅测试当前草稿；请求断开时取消本次任务，并等待客户端关闭"""
    resources = get_resources(request.app)
    result = await _connected_operation(
        request,
        lambda: run_model_test(
            resources.database,
            user_id=user.user_id,
            payload=payload,
            allowed_origins=resources.settings.model_allowed_origins,
        ),
    )
    return ApiResponse.success(result)


@router.post(
    "/connections/{connection_id}/models",
    response_model=ApiResponse[ModelDiscoveryResult],
)
async def connection_models(
    request: Request,
    connection_id: str,
    user: Annotated[UserContext, Depends(_test_user)],
) -> ApiResponse[ModelDiscoveryResult]:
    """获取本人连接的模型列表，网络等待不占用数据库连接，断连时取消"""
    resources = get_resources(request.app)
    async with resources.database.session() as session:
        connection = await AgentModelService(
            AgentModelRepository(session, user_id=user.user_id)
        ).require_connection(connection_id)
        provider = connection_provider(connection)
        base_url = connection.base_url
        api_key = connection.api_key

    async def discover() -> ModelDiscoveryResult:
        try:
            with anyio.fail_after(15):
                async with httpx.AsyncClient(
                    transport=ModelTransport(
                        allowed_origins=resources.settings.model_allowed_origins,
                        response_limit_bytes=4 * 1024 * 1024,
                    ),
                    timeout=10,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client:
                    return await discover_models(
                        provider,
                        base_url,
                        api_key,
                        client,
                    )
        except Exception as error:  # noqa: BLE001 - 供应商错误只返回固定原因
            return ModelDiscoveryResult(outcome="failed", code=_failure_code(error))

    return ApiResponse.success(await _connected_operation(request, discover))


_ResultT = TypeVar("_ResultT")


async def _connected_operation(
    request: Request, operation: Callable[[], Awaitable[_ResultT]]
) -> _ResultT:
    """拥有测试或发现请求，浏览器断连时取消并等待全部任务清理"""

    async def watch_disconnect() -> None:
        while not await request.is_disconnected():
            await asyncio.sleep(0.1)

    async def execute() -> _ResultT:
        return await operation()

    test = asyncio.create_task(execute())
    disconnected = asyncio.create_task(watch_disconnect())
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            (test, disconnected), return_when=asyncio.FIRST_COMPLETED
        )
        if test not in done:
            raise asyncio.CancelledError()
        return await test
    except BaseException as error:
        primary = error
        raise
    finally:
        tasks = (test, disconnected)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        cancellation = primary if isinstance(primary, asyncio.CancelledError) else None
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.wait(tasks)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                    if any(not task.done() for task in tasks):
                        continue
                break
        failures: list[BaseException] = [] if primary is None else [primary]
        if cancellation is not None and cancellation is not primary:
            failures.append(cancellation)
        for task in tasks:
            try:
                task.result()
            except BaseException as error:  # noqa: BLE001 - 收齐后一次性交付原始失败
                if error is not primary and _cleanup_failure_priority(error):
                    failures.append(error)
        if failures:
            chosen = next(
                (error for error in failures if _cleanup_failure_priority(error) == 2),
                cancellation or primary or failures[0],
            )
            remaining = [error for error in failures if error is not chosen]
            if remaining:
                secondary = (
                    remaining[0]
                    if len(remaining) == 1
                    else BaseExceptionGroup("模型测试与客户端清理同时失败", remaining)
                )
                try:
                    raise secondary
                except BaseException:  # noqa: BLE001 - 保留取消及各异常原始cause
                    raise chosen
            raise chosen
