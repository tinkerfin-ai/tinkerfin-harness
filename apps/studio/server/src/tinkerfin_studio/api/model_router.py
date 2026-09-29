"""前端可用模型目录路由"""

from typing import Annotated

import anyio
import httpx
from fastapi import APIRouter, Request
from pydantic import Field, TypeAdapter

from tinkerfin_studio.api.dependencies import (
    ModelServiceDep,
    NetworkUserDep,
    UserContextDep,
)
from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.api.network_operation import connected_operation
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.models.capabilities import resolve_image_input
from tinkerfin_studio.models.catalog import PROVIDER_PRESETS
from tinkerfin_studio.models.discovery import discover_models, discovery_failure_code
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelSave,
    AgentModelSettings,
    InputCapabilityRequest,
    InputCapabilityResult,
    ModelAPI,
    ModelConnectionSave,
    ModelConnectionSettings,
    ModelDiscoveryResult,
    ModelSettingsOverview,
    ProviderPreset,
)
from tinkerfin_studio.models.service import AgentModelService, connection_provider
from tinkerfin_studio.models.transport import ModelTransport
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/models", tags=["模型"])


@router.post("/input-capabilities", response_model=ApiResponse[InputCapabilityResult])
async def model_input_capabilities(
    payload: InputCapabilityRequest, service: ModelServiceDep
) -> ApiResponse[InputCapabilityResult]:
    """预览本人连接的图片输入资料，不保存或请求供应商"""
    return ApiResponse.success(
        InputCapabilityResult(
            image_input_capability=await service.input_capability(payload)
        )
    )


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
    """保存当前用户的对话模型，不能访问其他用户的同名配置"""
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
    """启用本人已保存模型并设为默认项，保持连接配置不变"""
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


@router.post(
    "/connections/{connection_id}/models",
    response_model=ApiResponse[ModelDiscoveryResult],
)
async def connection_models(
    request: Request,
    connection_id: str,
    user: NetworkUserDep,
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
        async with httpx.AsyncClient(
            transport=ModelTransport(
                response_limit_bytes=4 * 1024 * 1024,
            ),
            timeout=10,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as client:
            # 发现超时和供应商错误返回固定原因，客户端关闭失败继续传播
            try:
                with anyio.fail_after(15):
                    return await discover_models(
                        provider,
                        base_url,
                        api_key,
                        client,
                    )
            except Exception as error:  # noqa: BLE001 - 供应商错误只返回固定原因
                return ModelDiscoveryResult(
                    outcome="failed", code=discovery_failure_code(error)
                )

    result = await connected_operation(request, discover)
    for item in result.items:
        item.image_input_capability = resolve_image_input(
            provider_id=connection.provider_id,
            api_type=TypeAdapter(ModelAPI).validate_python(connection.api_type),
            base_url=base_url,
            model_name=item.model_name,
        )
    return ApiResponse.success(result)
