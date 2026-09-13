"""附件原件保留、权限、文档读取和删除边界"""

import base64
import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.attachments.storage import DiskAttachmentStorage
from tinkerfin_studio.conversation.models import ConversationThread


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, "PNG")
    return output.getvalue()


@pytest.fixture
async def attachments(database, tmp_path):
    return AttachmentService(database, DiskAttachmentStorage(tmp_path / "attachments"))


async def test_upload_keeps_original_and_rejects_other_users(attachments):
    data = png()
    file = await attachments.upload(
        user_id=1, name="截图.png", chunks=byte_chunks(data)
    )
    _, original = await attachments.read(file.id, user_id=1)
    assert original == data
    _, preview = await attachments.read(file.id, user_id=1, variant="preview")
    assert Image.open(io.BytesIO(preview)).format == "JPEG"
    with pytest.raises(BusinessException) as denied:
        await attachments.read(file.id, user_id=2)
    assert denied.value.error_code.http_status == 404


async def test_binding_prevents_cross_thread_reuse_and_draft_deletion(
    attachments, database
):
    file = await attachments.upload(
        user_id=1, name="chart.png", chunks=byte_chunks(png())
    )
    async with database.session() as session:
        session.add(
            ConversationThread(
                user_id=1,
                thread_id="thread-a",
                title="附件测试",
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()
        await attachments.bind(
            session, [file.id], user_id=1, thread_id="thread-a", message_id="message-a"
        )
        await session.commit()
    with pytest.raises(BusinessException):
        await attachments.get(file.id, user_id=1, thread_id="thread-b")
    with pytest.raises(BusinessException) as rejected:
        await attachments.remove_draft(file.id, user_id=1)
    assert rejected.value.error_code.http_status == 409
    assert (await attachments.list_thread(user_id=1, thread_id="thread-a"))[
        0
    ].id == file.id


async def test_cleanup_removes_expired_drafts_and_deleted_thread_files(
    attachments, database
):
    file = await attachments.upload(
        user_id=1, name="chart.png", chunks=byte_chunks(png())
    )
    async with database.session() as session:
        row = await session.get(AttachmentFile, file.id)
        row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
        await session.commit()
    await attachments.cleanup()
    with pytest.raises(BusinessException):
        await attachments.get(file.id, user_id=1)
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None


async def test_invalid_type_and_path_do_not_publish_files(attachments):
    for name, data in [
        ("../x.png", png()),
        ("fake.png", b"not an image"),
        ("file.exe", b"binary"),
    ]:
        with pytest.raises(BusinessException):
            await attachments.upload(user_id=1, name=name, chunks=byte_chunks(data))


async def test_generated_workbook_and_pdf_are_readable(attachments):
    workbook = await attachments.documents.run(
        {
            "operation": "generate",
            "kind": "xlsx",
            "rows": [["quarter", "revenue"], ["Q3", "128"]],
        }
    )
    result = await attachments.documents.run(
        {
            "operation": "read",
            "kind": "xlsx",
            "data": workbook["data"],
            "start": 1,
            "count": 2,
        }
    )
    assert result["rows"] == [["quarter", "revenue"], ["Q3", "128"]]
    pdf = await attachments.documents.run(
        {"operation": "generate", "kind": "pdf", "text": "第三季度营收：128 万元"}
    )
    assert base64.b64decode(pdf["data"]).startswith(b"%PDF")
    result = await attachments.documents.run(
        {"operation": "read", "kind": "pdf", "data": pdf["data"], "count": 1}
    )
    assert "128" in str(result)


async def test_cancelled_storage_write_removes_staging_record_and_bytes(
    database, tmp_path
):
    """取消不得留下可见附件或无法按记录清理的文件"""
    import asyncio

    class InterruptedStorage(DiskAttachmentStorage):
        async def put(self, key, chunks):
            async def interrupted():
                async for chunk in chunks:
                    yield chunk
                    raise asyncio.CancelledError()

            await super().put(key, interrupted())

    root = tmp_path / "cancelled"
    service = AttachmentService(database, InterruptedStorage(root))
    with pytest.raises(asyncio.CancelledError):
        await service.upload(user_id=1, name="cancelled.png", chunks=byte_chunks(png()))
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None
    assert not list(root.iterdir())


async def test_docx_paragraphs_and_tables_are_read(attachments):
    """Word 的正文与表格内容均能被附件工具读取"""
    from docx import Document

    document = Document()
    document.add_paragraph("订单编号 42")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "金额"
    table.cell(0, 1).text = "128"
    output = io.BytesIO()
    document.save(output)
    result = await attachments.documents.run(
        {
            "operation": "read",
            "kind": "docx",
            "data": base64.b64encode(output.getvalue()).decode("ascii"),
            "count": 10,
        }
    )
    assert "订单编号 42" in str(result)
    assert "金额 | 128" in str(result)


async def test_workbook_preserves_numbers_and_treats_formula_like_text_as_text(
    attachments,
):
    """生成的数据表保留数值类型，不将外部文字变成可执行公式"""
    from openpyxl import load_workbook

    result = await attachments.documents.run(
        {"operation": "generate", "kind": "xlsx", "rows": [[128, "=SUM(A1:A2)"]]}
    )
    workbook = load_workbook(io.BytesIO(base64.b64decode(result["data"])))
    sheet = workbook.active
    assert sheet is not None
    assert sheet["A1"].value == 128
    assert sheet["B1"].data_type == "s"
    workbook.close()


async def test_same_name_attachments_remain_distinct_after_service_restart(
    attachments, database, tmp_path
):
    """同名报告按 ID 区分，重新创建服务后原件和会话引用仍可读取"""
    from docx import Document

    files = []
    for marker in ("报告 A：收入 128", "报告 B：收入 256"):
        document = Document()
        document.add_paragraph(marker)
        output = io.BytesIO()
        document.save(output)
        files.append(
            await attachments.upload(
                user_id=1, name="report.docx", chunks=byte_chunks(output.getvalue())
            )
        )
    assert files[0].id != files[1].id
    async with database.session() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            ConversationThread(
                user_id=1,
                thread_id="reports",
                title="比较报告",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
        await attachments.bind(
            session,
            [file.id for file in files],
            user_id=1,
            thread_id="reports",
            message_id="question",
        )
        await session.commit()
    restored = AttachmentService(
        database, DiskAttachmentStorage(tmp_path / "attachments")
    )
    assert len(await restored.list_thread(user_id=1, thread_id="reports")) == 2
    results = []
    for file in files:
        _, data = await restored.read(file.id, user_id=1, thread_id="reports")
        results.append(
            await restored.documents.run(
                {
                    "operation": "read",
                    "kind": "docx",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        )
    assert "128" in str(results[0]) and "256" in str(results[1])


@pytest.mark.parametrize(
    "format_name, mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
async def test_generated_image_tool_preserves_actual_format_and_typed_result(
    attachments, database, monkeypatch, format_name, mime
):
    """供应商返回不同图片格式时仍可保存，真实工具调用保持结构化附件"""
    from langchain_core.messages import ToolMessage

    from tinkerfin_agui_adapter import DeepAgentAgUiAdapter, RunIdentity
    from tinkerfin_studio.attachments import tools as media_tools

    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, format_name)
    data = output.getvalue()

    async def generate(model, prompt, *, allowed_origins):
        assert allowed_origins == ("http://localhost:11434",)
        return data

    monkeypatch.setattr(media_tools, "generate_image_bytes", generate)
    async with database.session() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            ConversationThread(
                user_id=1,
                thread_id="generated",
                title="生成图片",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    tools = media_tools.build_attachment_tools(
        service=attachments,
        processor=attachments.documents,
        user_id=1,
        thread_id="generated",
        image_model=None,
        supports_images=True,
        model_allowed_origins=("http://localhost:11434",),
    )
    generator = next(item for item in tools if item.name == "generate_image")
    result = await generator.ainvoke(
        {
            "type": "tool_call",
            "id": "generate",
            "name": "generate_image",
            "args": {"prompt": "blue square"},
        }
    )
    assert isinstance(result, ToolMessage)
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="generated", run_id="run")
    )
    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (result, {"lc_agent_name": None, "langgraph_node": "tools"}),
        }
    )
    wire = next(
        item.model_dump(mode="json", by_alias=True)
        for item in events
        if item.type == "TOOL_CALL_RESULT"
    )
    assert wire["content"] == ""
    assert len(wire["attachments"]) == 1
    attachment = wire["attachments"][0]
    assert attachment["mime_type"] == mime
    _, original = await attachments.read(
        attachment["id"], user_id=1, thread_id="generated"
    )
    assert original == data


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "unknown", "kind": "png", "values": [1]},
        {"operation": "generate", "kind": "xlsx", "rows": [[{"unexpected": "object"}]]},
        {"operation": "read", "kind": "pdf", "data": "", "start": True},
    ],
)
async def test_document_worker_rejects_invalid_protocol_input(attachments, payload):
    with pytest.raises(ValueError):
        await attachments.documents.run(payload)


def test_attachment_tools_describe_all_model_visible_parameters(attachments):
    from tinkerfin_studio.attachments.tools import build_attachment_tools

    tools = build_attachment_tools(
        service=attachments,
        processor=attachments.documents,
        user_id=1,
        thread_id="schema-only",
        image_model=None,
        supports_images=True,
    )
    for tool in tools:
        schema_type = tool.get_input_schema()
        assert issubclass(schema_type, BaseModel)
        schema = schema_type.model_json_schema()
        assert all(
            field.get("description") for field in schema.get("properties", {}).values()
        )


@pytest.mark.parametrize("extension", ["md", "markdown", "MD"])
async def test_markdown_upload_keeps_original_encoding_and_reads_line_ranges(
    attachments, extension
):
    """Markdown 保留原件字节，读取接受 BOM 并按原文行号返回内容"""
    original = "\ufeff# 门店月报\r\n\r\n| 门店 | 营收 |\r\n| --- | --- |\r\n| 一店 | 128 |\r\n".encode()
    file = await attachments.upload(
        user_id=1, name=f"月报.{extension}", chunks=byte_chunks(original)
    )
    assert file.mime_type == "text/markdown"
    assert file.size_bytes == len(original)
    _, downloaded = await attachments.read(file.id, user_id=1)
    assert downloaded == original
    result = await attachments.documents.run(
        {
            "operation": "read",
            "kind": "md",
            "data": base64.b64encode(downloaded).decode("ascii"),
            "start": 2,
            "count": 2,
        }
    )
    assert result == {
        "start_line": 2,
        "lines": ["", "| 门店 | 营收 |"],
        "total_lines": 5,
    }
    with pytest.raises(BusinessException):
        await attachments.read(file.id, user_id=2)
    with pytest.raises(BusinessException):
        await attachments.read(file.id, user_id=1, variant="preview")


@pytest.mark.parametrize("data", [b"", b"\xff\xfe#\x00", b"# report\x00data"])
async def test_invalid_markdown_is_not_published(attachments, database, data):
    """空文件、非 UTF-8 和二进制内容不得留下可见附件"""
    with pytest.raises(BusinessException):
        await attachments.upload(user_id=1, name="report.md", chunks=byte_chunks(data))
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None


@pytest.mark.parametrize("extension", ["md", "markdown"])
async def test_markdown_tools_generate_deliver_read_and_reopen(
    attachments, database, tmp_path, extension
):
    """真实生成和读取工具交付同一 Markdown，服务重建后保留正文与会话权限"""
    import json

    from langchain_core.messages import ToolMessage

    from tinkerfin_contracts.media import attachment_from_block
    from tinkerfin_studio.attachments.tools import build_attachment_tools

    async with database.session() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            ConversationThread(
                user_id=1,
                thread_id="markdown-report",
                title="门店月报",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    tools = {
        item.name: item
        for item in build_attachment_tools(
            service=attachments,
            processor=attachments.documents,
            user_id=1,
            thread_id="markdown-report",
            image_model=None,
            supports_images=False,
        )
    }
    text = "# 门店月报\n\n- 营收：128 万元\n- 下月安排：优化排班\n"
    result = await tools["create_file"].ainvoke(
        {
            "type": "tool_call",
            "id": "create-report",
            "name": "create_file",
            "args": {"name": f"月报.{extension}", "kind": "md", "text": text},
        }
    )
    assert isinstance(result, ToolMessage)
    assert isinstance(result.content, list)
    file = attachment_from_block(result.content[0])
    assert file is not None and file.mime_type == "text/markdown"
    read = await tools["read_attachment"].ainvoke({"attachment_id": file.id})
    assert json.loads(read)["lines"] == text.splitlines()
    restored = AttachmentService(
        database, DiskAttachmentStorage(tmp_path / "attachments")
    )
    _, original = await restored.read(file.id, user_id=1, thread_id="markdown-report")
    assert original == text.encode()
    assert [
        item.id
        for item in await restored.list_thread(user_id=1, thread_id="markdown-report")
    ] == [file.id]
    with pytest.raises(BusinessException):
        await restored.read(file.id, user_id=1, thread_id="another-thread")
    with pytest.raises(BusinessException):
        await restored.read(file.id, user_id=2, thread_id="markdown-report")
