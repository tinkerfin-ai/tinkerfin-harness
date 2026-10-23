"""Model attachment budgets preserve usable references and source history."""

import asyncio
import base64
import json

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from test_compaction import RecordingModel

from tinkerfin import AttachmentContent, AttachmentSupport, TinkerFin
from tinkerfin_contracts.media import Attachment

PDF = b"%PDF-1.4\n%%EOF"


def pdf(index: int) -> Attachment:
    return Attachment(
        id=str(index),
        name=f"{index}.pdf",
        mime_type="application/pdf",
        size_bytes=len(PDF),
    )


def model() -> RecordingModel:
    return RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": True}
    )


def included_files(recording: RecordingModel) -> list[str]:
    return [
        attachment.id
        for message in recording.inputs[0]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict)
        if isinstance(extras := block.get("extras"), dict) and "attachment" in extras
        if (attachment := Attachment.model_validate(extras["attachment"]))
    ]


@pytest.mark.parametrize("across_turns", [False, True])
async def test_budget_prefers_recent_files_without_reordering_or_changing_history(
    across_turns,
):
    files = [pdf(i) for i in range(5 if across_turns else 3)]
    reads = []

    async def read(attachment):
        reads.append(attachment.id)
        return AttachmentContent(PDF, "application/pdf")

    messages: list[AnyMessage] = [
        HumanMessage(content=[item.content_block() for item in files])
    ]
    if across_turns:
        messages = [
            HumanMessage(content=[item.content_block() for item in files[:2]]),
            AIMessage(content="previous turn"),
            HumanMessage(content=[item.content_block() for item in files[2:]]),
        ]
    for index, message in enumerate(messages):
        message.id = f"message-{index}"
    original = [message.model_dump() for message in messages]
    recording = model()
    runtime = (
        TinkerFin()
        .with_namespace("attachment-budget")
        .with_attachments(AttachmentSupport(read_content=read, max_bytes=2 * len(PDF)))
        .build(model=recording)
    )
    state = await runtime.ainvoke(
        thread_id="thread", run_id="budget", input={"messages": [*messages]}
    )
    assert included_files(recording) == [item.id for item in files[-2:]]
    assert reads == [item.id for item in reversed(files)]
    assert "remaining attachment byte budget" in str(recording.inputs[0])
    assert [message.model_dump() for message in messages] == original
    assert base64.b64encode(PDF).decode() not in json.dumps(state, default=str)


async def test_file_larger_than_budget_does_not_prevent_an_older_file_from_fitting():
    small, large = pdf(0), pdf(1)

    async def read(attachment):
        return AttachmentContent(
            PDF * (3 if attachment.id == large.id else 1), "application/pdf"
        )

    recording = model()
    runtime = (
        TinkerFin()
        .with_namespace("attachment-budget")
        .with_attachments(AttachmentSupport(read_content=read, max_bytes=len(PDF)))
        .build(model=recording)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="fit",
        input={
            "messages": [
                HumanMessage(content=[small.content_block(), large.content_block()])
            ]
        },
    )
    assert included_files(recording) == [small.id]
    assert "1.pdf" in str(recording.inputs[0]) and "byte budget" in str(
        recording.inputs[0]
    )


async def test_count_limit_unsupported_and_missing_files_remain_distinct_references():
    files = [pdf(i) for i in range(3)]
    text = Attachment(id="text", name="data.csv", mime_type="text/csv", size_bytes=10)
    reads = []

    async def read(attachment):
        reads.append(attachment.id)
        if attachment.id == "2":
            raise FileNotFoundError(attachment.id)
        return AttachmentContent(PDF, "application/pdf")

    recording = model()
    runtime = (
        TinkerFin()
        .with_namespace("attachment-budget")
        .with_attachments(AttachmentSupport(read_content=read, max_attachments=2))
        .build(model=recording)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="references",
        input={
            "messages": [
                HumanMessage(
                    content=[
                        *(item.content_block() for item in files),
                        text.content_block(),
                        files[1].content_block(),
                    ]
                )
            ]
        },
    )
    assert reads == ["1", "2"] and included_files(recording) == ["1"]
    rendered = str(recording.inputs[0])
    assert "Original file is unavailable" in rendered
    assert "direct input of this file format is not enabled" in rendered
    assert "File remains stored" in rendered


@pytest.mark.parametrize("failure", ["permission", "mime"])
async def test_budget_never_converts_authorization_or_reader_mime_errors_to_references(
    failure,
):
    async def read(attachment):
        if failure == "permission":
            raise PermissionError("denied")
        return AttachmentContent(b"unexpected", "image/png")

    recording = model()
    runtime = (
        TinkerFin()
        .with_namespace("attachment-budget")
        .with_attachments(AttachmentSupport(read_content=read, max_bytes=1))
        .build(model=recording)
    )
    with pytest.raises(PermissionError if failure == "permission" else ValueError):
        await runtime.ainvoke(
            thread_id="thread",
            run_id="denied",
            input={"messages": [HumanMessage(content=[pdf(0).content_block()])]},
        )
    assert not recording.inputs


async def test_cancelled_attachment_read_finishes_cleanup_and_does_not_invoke_model():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def read(attachment):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return AttachmentContent(PDF, "application/pdf")

    recording = model()
    runtime = (
        TinkerFin()
        .with_namespace("attachment-budget")
        .with_attachments(AttachmentSupport(read_content=read))
        .build(model=recording)
    )
    task = asyncio.create_task(
        runtime.ainvoke(
            thread_id="thread",
            run_id="cancelled",
            input={"messages": [HumanMessage(content=[pdf(0).content_block()])]},
        )
    )
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not recording.inputs
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
