"""经过用户认证的附件直传、读取许可与草稿删除入口"""

from typing import Literal

from fastapi import APIRouter, Request, Response

from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.dependencies import UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.attachments.schemas import (
    DownloadResponse,
    UploadRequest,
    UploadResponse,
)
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/attachments", tags=["附件"])


@router.post("/uploads", response_model=ApiResponse[UploadResponse])
async def request_upload(
    body: UploadRequest,
    request: Request,
    response: Response,
    user: UserContextDep,
) -> ApiResponse[UploadResponse]:
    """申请限时上传表单，文件由浏览器直接发送到对象存储"""
    response.headers["Cache-Control"] = "private, no-store"
    service = get_resources(request.app).attachments
    await service.cleanup()
    attachment_id, form = await service.request_upload(
        user_id=user.user_id, name=body.name, size_bytes=body.size_bytes
    )
    return ApiResponse.success(
        UploadResponse(
            attachment_id=attachment_id,
            url=form.url,
            fields=form.fields,
            expires_in=form.expires_in,
        )
    )


@router.post("/{attachment_id}/complete", response_model=ApiResponse[Attachment])
async def complete_upload(
    attachment_id: str,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[Attachment]:
    """核验直传内容，返回可用于消息和任务的附件描述"""
    return ApiResponse.success(
        await get_resources(request.app).attachments.complete_upload(
            attachment_id, user_id=user.user_id
        )
    )


@router.get(
    "/{attachment_id}/download-url", response_model=ApiResponse[DownloadResponse]
)
async def download_url(
    attachment_id: str,
    request: Request,
    response: Response,
    user: UserContextDep,
    variant: Literal["original", "preview"] = "original",
) -> ApiResponse[DownloadResponse]:
    """鉴权后提供短期读取地址，链接有效期内可直接使用"""
    response.headers["Cache-Control"] = "private, no-store"
    link = await get_resources(request.app).attachments.download_url(
        attachment_id, user_id=user.user_id, variant=variant
    )
    return ApiResponse.success(
        DownloadResponse(url=link.url, expires_in=link.expires_in)
    )


@router.delete("/{attachment_id}", response_model=ApiResponse[None])
async def delete_attachment(
    attachment_id: str, request: Request, user: UserContextDep
) -> ApiResponse[None]:
    """删除尚未发送的本人附件"""
    await get_resources(request.app).attachments.remove_draft(
        attachment_id, user_id=user.user_id
    )
    return ApiResponse.success()
