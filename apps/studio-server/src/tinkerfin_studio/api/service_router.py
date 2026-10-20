"""个人网页搜索与图片生成服务设置接口"""

import base64

import httpx
from fastapi import APIRouter, Request

from tinkerfin_studio.api.dependencies import NetworkUserDep, ServiceConfigDep
from tinkerfin_studio.api.errors import BusinessException, ServiceErrorCode
from tinkerfin_studio.api.network_operation import connected_operation
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.models.transport import ModelResponseTooLarge
from tinkerfin_studio.resources import get_resources
from tinkerfin_studio.services.http import (
    SearchRequest,
    ServiceHTTPError,
    generate_image_bytes,
    image_extension,
    search_web,
)
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import (
    Capability,
    ServiceSave,
    ServiceSettings,
    ServiceTestResult,
)
from tinkerfin_studio.services.service import ServiceConfigService

router = APIRouter(prefix="/services", tags=["服务连接"])


@router.get(
    "/settings", response_model=ApiResponse[dict[Capability, ServiceSettings | None]]
)
async def service_settings(
    service: ServiceConfigDep,
) -> ApiResponse[dict[Capability, ServiceSettings | None]]:
    """一次读取本人搜索与生图配置，不回显密钥"""
    return ApiResponse.success(
        {
            "web_search": await service.settings("web_search"),
            "image_generation": await service.settings("image_generation"),
        }
    )


@router.put("/{capability}", response_model=ApiResponse[ServiceSettings])
async def save_service(
    capability: Capability, payload: ServiceSave, service: ServiceConfigDep
) -> ApiResponse[ServiceSettings]:
    """只校验并保存本人唯一服务配置，不调用供应商"""
    return ApiResponse.success(await service.save(capability, payload))


@router.delete("/{capability}", response_model=ApiResponse[None])
async def clear_service(
    capability: Capability, service: ServiceConfigDep
) -> ApiResponse[None]:
    """清除本人服务及凭证，新运行进入未配置状态"""
    await service.clear(capability)
    return ApiResponse.success()


def _test_failure_code(error: Exception) -> str:
    if isinstance(error, ServiceHTTPError):
        if error.status_code in {401, 403}:
            return "authentication_failed"
        if error.status_code == 429:
            return "rate_limited"
        return "service_error"
    if isinstance(error, ModelResponseTooLarge):
        return "response_too_large"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "network_error"
    return "invalid_response"


@router.post("/{capability}/test", response_model=ApiResponse[ServiceTestResult])
async def test_service(
    request: Request, capability: Capability, user: NetworkUserDep
) -> ApiResponse[ServiceTestResult]:
    """按本人已保存配置执行一次主动测试，可能消耗服务额度"""
    resources = get_resources(request.app)
    async with resources.database.session() as session:
        service = ServiceConfigService(
            ServiceConfigRepository(session, user_id=user.user_id)
        )
        settings = await service.settings(capability)
        if settings is None:
            raise BusinessException(ServiceErrorCode.NOT_FOUND)
        if not settings.enabled:
            raise BusinessException(ServiceErrorCode.DISABLED)
        resolved = await service.resolve(capability)
    if resolved is None:
        raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)

    async def execute() -> None:
        if capability == "web_search":
            await search_web(
                resolved, SearchRequest("TinkerFin test query", max_results=1)
            )
        else:
            data = await generate_image_bytes(
                resolved, "A small blue circle on a white background"
            )
            image_extension(data)
            await resources.attachments.documents.run(
                {
                    "operation": "preview_image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "max_bytes": 1024 * 1024,
                }
            )

    result = ServiceTestResult(outcome="success", code="success")
    try:
        await connected_operation(request, execute)
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as error:
        result = ServiceTestResult(outcome="failed", code=_test_failure_code(error))
    async with resources.database.session() as session:
        service = ServiceConfigService(
            ServiceConfigRepository(session, user_id=user.user_id)
        )
        await service.record_test(resolved, status=result.outcome, code=result.code)
    return ApiResponse.success(result)
