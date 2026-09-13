"""有界图片预处理、Markdown 文本与 Office 文件容器校验"""

from __future__ import annotations

import io
import warnings
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 5
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
    "md": "text/markdown",
    "markdown": "text/markdown",
}


def markdown_text(data: bytes) -> str:
    """读取 UTF-8 Markdown，接受字节顺序标记并拒绝二进制空字符"""
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("Markdown 文件必须是 UTF-8 文本")
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
    """结合文件内容和扩展名核验类型，限制 Office 解压容量"""
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("附件不能为空，且单个不能超过 10 MiB")
    extension = name.rsplit(".", 1)[-1].lower()
    mime = MIME_TYPES.get(extension)
    if mime is None:
        raise ValueError("仅支持图片、Markdown、PDF、DOCX 和 XLSX")
    if mime.startswith("image/"):
        with Image.open(io.BytesIO(data)) as image:
            actual = Image.MIME.get(image.format or "")
            if actual != mime:
                raise ValueError("图片内容与扩展名不一致")
        image_variant(data, 2048)
    elif mime == "text/markdown":
        markdown_text(data)
    elif extension == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("PDF 文件内容不正确")
    else:
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if (
                    len(members) > 2000
                    or sum(m.file_size for m in members) > 50 * 1024 * 1024
                ):
                    raise ValueError("文档解压后过大，请拆分文件")
                expected = (
                    "word/document.xml" if extension == "docx" else "xl/workbook.xml"
                )
                if expected not in archive.namelist() or any(
                    m.flag_bits & 1 for m in members
                ):
                    raise ValueError("文档损坏或已加密")
        except BadZipFile as error:
            raise ValueError("文档容器损坏") from error
    return mime
