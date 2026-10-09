"""用户头像的有界接收与图片处理"""

import io

import anyio
from PIL import Image, UnidentifiedImageError

from tinkerfin_studio.api.errors import BusinessException, GlobalErrorCode
from tinkerfin_studio.attachments.processing import image_variant
from tinkerfin_studio.infrastructure.threads import run_owned_thread

MAX_AVATAR_BYTES = 5 * 1024 * 1024
_decoders = anyio.CapacityLimiter(2)


def _image(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
            raise ValueError("头像图片格式不支持")
    return image_variant(data, 256)


async def prepare_avatar(data: bytes) -> bytes:
    """缩小头像并移除原始元数据，最多两个线程解码

    复用附件图片处理的像素上限和首帧规则。同步图片库不能中途取消，
    取消时等待已启动的有界处理结束，线程不会脱离请求继续写入资料。

    Args:
        data: 不超过 5 MiB 的 PNG、JPEG、WebP 或 GIF 原始图片

    Returns:
        最长边不超过 256 像素且不包含原始元数据的 JPEG 内容

    Raises:
        BusinessException: 图片格式、大小或像素数量不符合要求
    """

    if not data or len(data) > MAX_AVATAR_BYTES:
        raise BusinessException(
            GlobalErrorCode.VALIDATION_FAILED, message="请选择不超过 5 MiB 的头像图片"
        )
    try:
        return await run_owned_thread(_image, data, limiter=_decoders)
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise BusinessException(
            GlobalErrorCode.VALIDATION_FAILED,
            message="图片无法读取，请选择 PNG、JPEG、WebP 或 GIF 图片",
        ) from error
