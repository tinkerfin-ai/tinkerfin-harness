"""附件类型、原件边界与已有预览格式的内容校验"""

from __future__ import annotations

import io
import warnings
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "zip": "application/zip",
    "csv": "text/csv",
    "txt": "text/plain",
    "json": "application/json",
    "html": "text/html",
    "htm": "text/html",
    "py": "text/x-python",
}


class FileValidationError(ValueError):
    """可直接告知用户的附件名称或内容错误"""


def validate_name(name: str) -> None:
    """校验下载文件名，不把扩展名或文本编码作为收发许可"""
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or any(c in {"/", "\\"} or ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise FileValidationError("文件名须为不含目录和控制字符的名称，最多 255 个字符")


def file_mime(name: str) -> str:
    """为支持处理的格式声明类型，其余文件保留原件供下载或工具读取"""
    return MIME_TYPES.get(
        PurePosixPath(name).suffix[1:].lower(), "application/octet-stream"
    )


def markdown_text(data: bytes) -> str:
    """读取 UTF-8 Markdown，接受字节顺序标记并拒绝二进制空字符"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FileValidationError(
            "Markdown 文字编码无法识别，请转换为 UTF-8 后上传"
        ) from error
    if "\x00" in text:
        raise FileValidationError("Markdown 文件包含二进制空字符")
    return text


def image_variant(data: bytes, max_side: int) -> bytes:
    """提取首帧并缩放，保留原件不变，超出像素限制时拒绝处理"""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("图片像素过多，请缩小后重试")
            original.seek(0)
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((max_side, max_side))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85)
            return output.getvalue()


def validate_file(name: str, data: bytes) -> str:
    """保留通用文件字节，只解析已有预览格式；技能校验归属技能导入"""
    validate_name(name)
    if not data:
        raise FileValidationError("附件不能为空")
    if len(data) > MAX_FILE_BYTES:
        raise FileValidationError("单个附件不能超过 10 MiB")
    extension = PurePosixPath(name).suffix[1:].lower()
    mime = file_mime(name)
    if mime.startswith("image/"):
        try:
            with Image.open(io.BytesIO(data)) as image:
                actual = Image.MIME.get(image.format or "")
                if actual != mime:
                    raise FileValidationError("图片内容与扩展名不一致")
            image_variant(data, 2048)
        except FileValidationError:
            raise
        except (OSError, ValueError, Warning, Image.DecompressionBombError) as error:
            raise FileValidationError(
                "图片内容无法识别或超过像素限制，请检查原文件"
            ) from error
    elif mime == "text/markdown":
        markdown_text(data)
    elif extension == "pdf":
        if not data.startswith(b"%PDF-"):
            raise FileValidationError("PDF 文件内容不正确")
    elif extension in {"docx", "xlsx", "pptx"}:
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if (
                    len(members) > 2000
                    or sum(m.file_size for m in members) > 50 * 1024 * 1024
                ):
                    raise FileValidationError("文档解压后过大，请拆分文件")
                expected = {
                    "docx": "word/document.xml",
                    "xlsx": "xl/workbook.xml",
                    "pptx": "ppt/presentation.xml",
                }[extension]
                if expected not in archive.namelist() or any(
                    m.flag_bits & 1 for m in members
                ):
                    raise FileValidationError("文档损坏或已加密")
        except BadZipFile as error:
            raise FileValidationError("文档容器损坏") from error
    return mime
