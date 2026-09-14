"""在可终止的独立进程内读取文档和生成交付文件"""

from __future__ import annotations

import base64
import io
import json
import math
import resource
import sys
from typing import Annotated, Literal
from xml.sax.saxutils import escape

from docx import Document
from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pypdf import PdfReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from tinkerfin_studio.attachments.processing import (
    MAX_FILE_BYTES,
    image_variant,
    markdown_text,
)


class _ReadDocument(BaseModel):
    """子进程读取请求，限制文件类型和一次读取的范围"""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["read"]
    kind: Literal["pdf", "docx", "xlsx", "md"]
    data: str = Field(max_length=14_000_000, description="原文件的Base64内容")
    start: int = Field(
        default=1, ge=1, strict=True, description="从1开始的页、段落或行号"
    )
    count: int = Field(default=20, ge=1, le=100, strict=True)
    sheet: str | None = Field(default=None, max_length=255)


class _GenerateDocument(BaseModel):
    """子进程生成请求，只接收有界文字、单元格与图表数值"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation: Literal["generate"]
    kind: Literal["xlsx", "pdf", "png", "md"]
    text: str = Field(default="", max_length=50_000)
    rows: (
        list[Annotated[list[str | int | float | bool | None], Field(max_length=50)]]
        | None
    ) = Field(default=None, max_length=1000)
    values: list[Annotated[float, Field(ge=0, le=1e12)]] | None = Field(
        default=None, min_length=1, max_length=30
    )


class _ImagePreview(BaseModel):
    """为工作图片生成不超过模型读取限制的首帧预览"""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["preview_image"]
    data: str = Field(max_length=14_000_000, description="原图片的 Base64 内容")
    max_bytes: int = Field(
        gt=0, le=MAX_FILE_BYTES, strict=True, description="预览允许的最大字节数"
    )


_DOCUMENT_REQUEST = TypeAdapter(
    Annotated[
        _ReadDocument | _GenerateDocument | _ImagePreview,
        Field(discriminator="operation"),
    ]
)


def preview_image(payload: _ImagePreview) -> dict[str, JsonValue]:
    """生成有界 JPEG 缩略图，不修改原图"""
    data = base64.b64decode(payload.data, validate=True)
    for max_side in (1280, 960, 640, 320):
        preview = image_variant(data, max_side)
        if len(preview) <= payload.max_bytes:
            return {"data": base64.b64encode(preview).decode("ascii")}
    raise ValueError("图片预览超过读取限制")


def read_document(payload: _ReadDocument) -> dict[str, JsonValue]:
    """只提取指定页或单元格范围，最终输出受子进程总量限制"""
    stream = io.BytesIO(base64.b64decode(payload.data, validate=True))
    kind = payload.kind
    start = payload.start
    count = payload.count
    if (
        not isinstance(start, int)
        or not isinstance(count, int)
        or start < 1
        or count < 1
        or count > 100
    ):
        raise ValueError("读取起点和数量不合法，最多读取 100 项")
    if kind == "pdf":
        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF")
        if len(reader.pages) > 500:
            raise ValueError("PDF 超过 500 页，请拆分文件")
        pages: list[JsonValue] = []
        for i in range(start - 1, min(start - 1 + min(count, 10), len(reader.pages))):
            text = reader.pages[i].extract_text() or ""
            if not text.strip():
                raise ValueError(f"第 {i + 1} 页没有可提取文字，扫描版 PDF 暂不支持")
            pages.append({"page": i + 1, "text": text[:5000]})
        return {"pages": pages, "total_pages": len(reader.pages)}
    if kind == "docx":
        doc = Document(stream)
        lines = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            lines.extend(" | ".join(c.text for c in row.cells) for row in table.rows)
        return {
            "paragraphs": [
                text[:1000] for text in lines[start - 1 : start - 1 + count]
            ],
            "total_paragraphs": len(lines),
        }
    if kind == "md":
        lines = markdown_text(stream.getvalue()).splitlines()
        return {
            "start_line": start,
            "lines": [line for line in lines[start - 1 : start - 1 + count]],
            "total_lines": len(lines),
        }
    if kind == "xlsx":
        workbook = load_workbook(stream, read_only=True, data_only=True)
        try:
            sheet = workbook[payload.sheet or workbook.sheetnames[0]]
            rows: list[JsonValue] = [
                [str(v)[:1000] if v is not None else "" for v in row]
                for row in sheet.iter_rows(
                    min_row=start,
                    max_row=start + count - 1,
                    max_col=20,
                    values_only=True,
                )
            ]
            for row in rows:
                while isinstance(row, list) and row and row[-1] == "":
                    row.pop()
            return {
                "sheet": sheet.title,
                "sheets": [name for name in workbook.sheetnames],
                "start_row": start,
                "rows": rows,
                "note": "公式使用文件保存时的缓存值，不执行重算；最多返回 20 列",
            }
        finally:
            workbook.close()
    raise ValueError("当前文件不支持文档读取")


def generate(payload: _GenerateDocument) -> dict[str, JsonValue]:
    """生成 Markdown、XLSX、PDF 或 PNG，限制输入规模"""
    kind = payload.kind
    output = io.BytesIO()
    if kind == "xlsx":
        rows = payload.rows
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("表格最多 1000 行")
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("数据")
        for row in rows:
            if not isinstance(row, list) or len(row) > 50:
                raise ValueError("表格最多 50 列")
            cells = []
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("表格数值必须为有限数")
                cell = WriteOnlyCell(
                    sheet, value=value[:2000] if isinstance(value, str) else value
                )
                if isinstance(value, str):
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        workbook.save(output)
    elif kind == "md":
        output.write(payload.text.encode("utf-8"))
    elif kind == "pdf":
        text = payload.text
        if len(text) > 50_000:
            raise ValueError("报告文字超过限制")
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        style = ParagraphStyle(
            "report", fontName="STSong-Light", fontSize=11, leading=18, wordWrap="CJK"
        )
        content = []
        for line in text.splitlines():
            content.extend([Paragraph(escape(line) or " ", style), Spacer(1, 6)])
        SimpleDocTemplate(output).build(content)
    elif kind == "png":
        values = payload.values
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 30
            or not all(isinstance(v, (int, float)) and 0 <= v <= 1e12 for v in values)
        ):
            raise ValueError("图表需要 1 到 30 个非负数值")
        image = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.line((60, 40, 60, 540, 960, 540), fill="#61666b", width=2)
        maximum = max(values) or 1
        width = 850 / len(values)
        for index, value in enumerate(values):
            left = 80 + index * width
            top = 530 - 440 * value / maximum
            draw.rectangle((left, top, left + width * 0.7, 538), fill="#3158e6")
            draw.text((left, top - 18), str(value), fill="#111111")
            draw.text((left, 552), str(index + 1), fill="#111111")
        image.save(output, "PNG")
    else:
        raise ValueError("生成类型只支持 Markdown、XLSX、PDF 或 PNG")
    data = output.getvalue()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("生成文件超过 10 MiB")
    return {"data": base64.b64encode(data).decode("ascii")}


def main() -> None:
    """执行一个标准输入任务并输出有界 JSON，不读取用户任意路径"""
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (25, 26))
        if sys.platform.startswith("linux"):
            resource.setrlimit(
                resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024)
            )
        payload = _DOCUMENT_REQUEST.validate_json(
            sys.stdin.buffer.read(20 * 1024 * 1024)
        )
        if isinstance(payload, _ReadDocument):
            result = read_document(payload)
        elif isinstance(payload, _ImagePreview):
            result = preview_image(payload)
        else:
            result = generate(payload)
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded) > (
            100_000 if isinstance(payload, _ReadDocument) else 15_000_000
        ):
            raise ValueError("输出超过限制，请缩小读取范围")
        print(encoded)
    except Exception:  # noqa: BLE001 - 子进程边界只向用户返回文件错误，不泄露内部路径
        print(
            json.dumps(
                {"error": "文件损坏、加密、扫描件或内容超限，请检查文件并缩小读取范围"},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
