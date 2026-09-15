"""可取消、有超时和并发限制的文档处理入口"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import anyio
from pydantic import JsonValue, TypeAdapter

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class ReadContext:
    """一次有界文档读取的输入和定位范围"""

    name: str
    mime_type: str
    data: bytes
    start: int
    count: int


class DocumentReader(Protocol):
    """把一种已确认格式转换成 Agent 可读取的有界文本结果"""

    def read(self, context: ReadContext) -> dict[str, JsonValue]:
        """读取指定范围的文档内容"""
        ...


class DoclingReader:
    """使用 Docling 把一种输入格式转换成 Markdown 行片段"""

    def __init__(self, input_format_name: str) -> None:
        self._input_format_name = input_format_name

    def read(self, context: ReadContext) -> dict[str, JsonValue]:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import NativePdfPipelineOptions
        from docling.document_converter import (
            DocumentConverter,
            FormatOption,
            NativePdfFormatOption,
        )

        input_format = getattr(InputFormat, self._input_format_name)
        format_options: dict[InputFormat, FormatOption] | None = (
            {
                InputFormat.PDF: NativePdfFormatOption(
                    pipeline_options=NativePdfPipelineOptions()
                )
            }
            if input_format is InputFormat.PDF
            else None
        )
        converter = DocumentConverter(
            allowed_formats=[input_format], format_options=format_options
        )
        source = DocumentStream(name=context.name, stream=BytesIO(context.data))
        result = converter.convert(
            source,
            raises_on_error=True,
            max_file_size=10 * 1024 * 1024,
            max_num_pages=500,
        )
        markdown = result.document.export_to_markdown()
        lines = markdown.splitlines()
        selected: list[JsonValue] = []
        for line in lines[context.start - 1 : context.start - 1 + context.count]:
            selected.append(line)
        return {
            "mime_type": context.mime_type,
            "start_line": context.start,
            "lines": selected,
            "total_lines": len(lines),
        }


class MarkdownReader:
    """按原始行读取 Markdown，保留既有行号和文本契约"""

    def read(self, context: ReadContext) -> dict[str, JsonValue]:
        from tinkerfin_studio.attachments.processing import markdown_text

        lines = markdown_text(context.data).splitlines()
        selected: list[JsonValue] = []
        for line in lines[context.start - 1 : context.start - 1 + context.count]:
            selected.append(line)
        return {
            "start_line": context.start,
            "lines": selected,
            "total_lines": len(lines),
        }


@dataclass(frozen=True, slots=True)
class ReaderSpec:
    """把服务端确认的 MIME 类型绑定到文档读取器"""

    mime_type: str
    factory: Callable[[], DocumentReader]


class ReaderRegistry:
    """管理 MIME 类型到读取器的唯一映射"""

    def __init__(self, specs: tuple[ReaderSpec, ...]) -> None:
        factories: dict[str, Callable[[], DocumentReader]] = {}
        for spec in specs:
            if spec.mime_type in factories:
                raise ValueError(f"重复的文档读取 MIME 类型：{spec.mime_type}")
            factories[spec.mime_type] = spec.factory
        self._factories = factories

    def create(self, mime_type: str) -> DocumentReader:
        """按已确认的 MIME 类型创建读取器"""
        try:
            return self._factories[mime_type]()
        except KeyError as error:
            raise ValueError(f"当前文件类型不支持文档读取：{mime_type}") from error


DOCUMENT_READERS = ReaderRegistry(
    (
        ReaderSpec("text/markdown", MarkdownReader),
        ReaderSpec("application/pdf", lambda: DoclingReader("PDF")),
        ReaderSpec(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            lambda: DoclingReader("DOCX"),
        ),
        ReaderSpec(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            lambda: DoclingReader("XLSX"),
        ),
        ReaderSpec(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            lambda: DoclingReader("PPTX"),
        ),
    )
)


class DocumentProcessor:
    """拥有每次文档子进程，超时或取消时终止并等待回收，最多并行两个"""

    def __init__(self) -> None:
        self._capacity = anyio.CapacityLimiter(2)

    async def run(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """处理一个有界文档任务，不把解析阻塞传播到 HTTP 或 Agent 事件循环"""
        async with self._capacity:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tinkerfin_studio.attachments.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                with anyio.fail_after(30):
                    stdout, _ = await process.communicate(
                        json.dumps(payload, ensure_ascii=False).encode()
                    )
                result = _JSON_OBJECT.validate_json(stdout)
                if process.returncode != 0:
                    raise ValueError(str(result.get("error", "文件处理失败")))
                return result
            finally:
                if process.returncode is None:
                    process.kill()
                with anyio.CancelScope(shield=True):
                    await process.wait()
