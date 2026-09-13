"""Attachment projection preserves durable content and provider tool pairing."""

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import UserMessage
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import Field, JsonValue, TypeAdapter

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport
from tinkerfin_agui_adapter.media import user_content_to_agui, user_message_to_langchain
from tinkerfin_tracing.redaction import RedactionContext, secure_redact

FILE = Attachment(id="sample", name="chart.png", mime_type="image/png", size_bytes=4)


@pytest.mark.parametrize(
    "schema_type", [{"enum": ["draft", "clarify"]}, ["string", "null"]]
)
def test_trace_preserves_schema_types_while_removing_attachment_bytes(schema_type):
    value = {
        "schema": {"properties": {"type": schema_type}},
        "content": [
            {
                "type": "image",
                "base64": "private-request-bytes",
                "extras": {"attachment": FILE.model_dump(mode="json", by_alias=True)},
            }
        ],
    }
    safe = secure_redact(
        value, context=RedactionContext(content_kind="model_request"), redactor=None
    )
    assert safe == {"schema": value["schema"], "content": [FILE.content_block()]}


class _CaptureModel(FakeMessagesListChatModel):
    seen: list[BaseMessage] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[BaseTool | dict[str, Any] | type | Callable[..., Any]],
        **kwargs: Any,
    ) -> Runnable:
        return self

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen[:] = [m for m in messages if not isinstance(m, SystemMessage)]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


async def _project(support, request, handler, *, direct=True):
    """Observe provider input through the complete public Graph construction path."""
    model = _CaptureModel(
        responses=[AIMessage(content="done")], profile=request.model.profile
    )
    initial = _CaptureModel(
        responses=[AIMessage(content="must not run")], profile={"image_inputs": False}
    )

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            return await handler(request.override(model=model))

    runtime = (
        TinkerFin()
        .with_namespace("attachments")
        .with_attachments(support)
        .build(model=initial, middleware=[Route()])
    )
    if direct:
        graph = await create_graph(runtime)
        result = await graph.ainvoke({"messages": request.messages})
    else:
        result = await runtime.ainvoke(
            thread_id="thread", run_id="run", input={"messages": request.messages}
        )
    assert not initial.seen
    assert "data:image" not in str(result)
    assert "tinkerfin_attachment" not in str(result)
    return await handler(request.override(model=model, messages=model.seen))


async def _capture_response(request):
    return ModelResponse(result=request.messages)


def test_agui_attachment_roundtrip():
    original = HumanMessage(
        content=[{"type": "text", "text": "read"}, FILE.content_block()], id="m"
    )
    wire = UserMessage.model_validate(
        {"id": "m", "content": user_content_to_agui(original.content)}
    )
    assert user_message_to_langchain(wire).content == original.content


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_images", [True, False])
async def test_model_projection_preserves_original_and_tool_order(supports_images):
    original = [
        AIMessage(content="", tool_calls=[{"id": "t", "name": "chart", "args": {}}]),
        ToolMessage(content=[FILE.content_block()], tool_call_id="t"),
    ]
    reads = []

    async def read_image(attachment):
        reads.append(attachment.id)
        return AttachmentContent(data=b"test", mime_type="image/png")

    projected = []

    async def handler(request):
        projected.extend(request.messages)
        return ModelResponse(result=[AIMessage(content="done")])

    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]), messages=original, tools=[]
    )
    await _project(
        AttachmentSupport(
            read_content=read_image,
            supports_content=lambda model, mime_type: supports_images,
        ),
        request,
        handler,
    )
    assert original[1].content == [FILE.content_block()]
    assert isinstance(projected[1], ToolMessage)
    assert len(reads) == int(supports_images)
    if supports_images:
        assert isinstance(projected[-1], HumanMessage)
        safe = secure_redact(
            TypeAdapter(JsonValue).validate_python(projected[-1].content),
            context=RedactionContext(content_kind="model_request"),
            redactor=None,
        )
        assert isinstance(safe, list)
        assert safe[-1] == FILE.content_block()
        assert "dGVzdA==" not in str(safe)
    else:
        assert "does not support this file format" in str(projected[1].content)


@pytest.mark.asyncio
async def test_missing_history_image_is_explicit_and_does_not_mutate_history():
    """An unavailable file must not be represented as an image the model saw."""

    async def missing(attachment):
        raise FileNotFoundError(attachment.id)

    message = HumanMessage(content=[FILE.content_block()])
    projected = await _project(
        AttachmentSupport(
            read_content=missing, supports_content=lambda model, mime_type: True
        ),
        ModelRequest(
            model=FakeListChatModel(responses=["done"]), messages=[message], tools=[]
        ),
        _capture_response,
    )
    assert isinstance(projected, ModelResponse)
    projected = projected.result
    assert "unavailable" in str(projected[0].content)
    assert message.content == [FILE.content_block()]


@pytest.mark.asyncio
async def test_real_tool_return_keeps_attachment_as_a_content_block():
    """Real tool invocation preserves structured attachments for downstream adapters."""
    from langchain_core.tools import tool

    from tinkerfin_contracts.media import attachment_from_block

    @tool
    async def picture():
        """Return a stored picture."""
        return [FILE.content_block()]

    result = await picture.ainvoke(
        {"type": "tool_call", "id": "call", "name": "picture", "args": {}}
    )
    assert isinstance(result, ToolMessage)
    assert isinstance(result.content, list)
    assert attachment_from_block(result.content[0]) == FILE


def test_invalid_request_image_metadata_uses_safe_trace_error():
    from tinkerfin_tracing import TraceCaptureRejected

    with pytest.raises(TraceCaptureRejected) as rejected:
        secure_redact(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,private"},
                "extras": {"attachment": {"id": "private"}},
            },
            context=RedactionContext(content_kind="model_request"),
            redactor=None,
        )
    assert "private" not in str(rejected.value)
    assert rejected.value.__cause__ is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_image_resolution_cancellation_preserves_input_messages(direct):
    import asyncio

    entered = asyncio.Event()
    closed = asyncio.Event()
    pending = asyncio.Event()

    async def read_image(attachment):
        entered.set()
        try:
            await pending.wait()
            raise AssertionError("reader must be cancelled")
        finally:
            closed.set()

    original = HumanMessage(content=[FILE.content_block()])
    task = asyncio.create_task(
        _project(
            AttachmentSupport(
                read_content=read_image, supports_content=lambda model, mime_type: True
            ),
            ModelRequest(
                model=FakeListChatModel(responses=["done"]),
                messages=[original],
                tools=[],
            ),
            _capture_response,
            direct=direct,
        )
    )
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert original.content == [FILE.content_block()]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_capabilities_follow_actual_model_and_authorization_is_independent(
    direct,
):
    vision = FakeListChatModel(responses=["done"], profile={"image_inputs": True})
    text = FakeListChatModel(responses=["done"], profile={"image_inputs": False})
    unknown = FakeListChatModel(responses=["done"])
    reads = []

    async def denied(attachment):
        reads.append(attachment.id)
        raise PermissionError("attachment is not authorized")

    support = AttachmentSupport(read_content=denied)
    original = HumanMessage(content=[FILE.content_block()])
    for model in [text, unknown]:
        result = await _project(
            support,
            ModelRequest(model=model, messages=[original], tools=[]),
            _capture_response,
            direct=direct,
        )
        assert isinstance(result, ModelResponse)
        assert "does not support this file format" in str(result.result[0].content)
    assert not reads
    with pytest.raises(PermissionError, match="not authorized"):
        await _project(
            support,
            ModelRequest(model=vision, messages=[original], tools=[]),
            _capture_response,
            direct=direct,
        )
    assert reads == [FILE.id]
    assert original.content == [FILE.content_block()]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", ["yes", 1, None])
async def test_host_model_capability_override_rejects_non_boolean_results(
    invalid_value,
):

    async def read_image(attachment):
        pytest.fail("invalid capability must not resolve images")

    support = AttachmentSupport(
        read_content=read_image, supports_content=lambda model, mime_type: invalid_value
    )
    with pytest.raises(TypeError, match="return a boolean"):
        await _project(
            support,
            ModelRequest(
                model=FakeListChatModel(responses=["done"]),
                messages=[HumanMessage(content=[FILE.content_block()])],
                tools=[],
            ),
            _capture_response,
        )


@pytest.mark.parametrize(
    ("mime_type", "wire_type"),
    [
        ("image/png", "image"),
        ("audio/wav", "audio"),
        ("video/mp4", "video"),
        ("application/pdf", "document"),
        ("application/zip", "document"),
        ("text/csv", "document"),
    ],
)
def test_file_references_roundtrip_without_assuming_document_format(
    mime_type, wire_type
):
    from tinkerfin.agui_input import AgUiUserInput

    attachment = Attachment(id="file", name="sample", mime_type=mime_type, size_bytes=4)
    original = HumanMessage(content=[attachment.content_block()], id="message")
    wire = UserMessage.model_validate(
        {"id": "message", "content": user_content_to_agui(original.content)}
    )
    assert not isinstance(wire.content, str)
    assert wire.content[0].type == wire_type
    assert user_message_to_langchain(wire).content == original.content
    submission = AgUiUserInput(content=wire.content)
    assert submission.attachment_ids == (attachment.id,)
    assert submission.with_attachments([attachment]).attachments == (attachment,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime_type", "capability", "block_type"),
    [
        ("image/png", "image_inputs", "image_url"),
        ("audio/wav", "audio_inputs", "audio"),
        ("video/mp4", "video_inputs", "video"),
        ("application/pdf", "pdf_inputs", "file"),
    ],
)
async def test_media_projection_keeps_bytes_out_of_history_and_trace(
    mime_type, capability, block_type
):
    attachment = Attachment(id="file", name="sample", mime_type=mime_type, size_bytes=4)
    original = [
        AIMessage(content="", tool_calls=[{"id": "t", "name": "file", "args": {}}]),
        ToolMessage(content=[attachment.content_block()], tool_call_id="t"),
    ]
    reads = []

    async def read_content(item):
        reads.append(item.id)
        return AttachmentContent(data=b"test", mime_type=mime_type)

    support = AttachmentSupport(read_content=read_content)
    for enabled in (False, True):
        model = FakeListChatModel(
            responses=["done"],
            profile={
                "image_inputs": enabled and capability == "image_inputs",
                "audio_inputs": enabled and capability == "audio_inputs",
                "video_inputs": enabled and capability == "video_inputs",
                "pdf_inputs": enabled and capability == "pdf_inputs",
            },
        )
        response = await _project(
            support,
            ModelRequest(model=model, messages=original, tools=[]),
            _capture_response,
        )
        assert isinstance(response, ModelResponse)
        assert isinstance(response.result[1], ToolMessage)
        assert response.result[1].tool_call_id == "t"
        assert original[1].content == [attachment.content_block()]
        if enabled:
            assert isinstance(response.result[-1], HumanMessage)
            block = response.result[-1].content[-1]
            assert isinstance(block, dict)
            assert block["type"] == block_type
            if block_type != "image_url":
                assert block["base64"] == "dGVzdA=="
                assert block["mime_type"] == mime_type
                if block_type == "file":
                    assert block["extras"]["filename"] == attachment.name
            safe = secure_redact(
                TypeAdapter(JsonValue).validate_python(block),
                context=RedactionContext(content_kind="model_request"),
                redactor=None,
            )
            assert safe == attachment.content_block()
        else:
            assert "does not support this file format" in str(
                response.result[1].content
            )
    assert reads == [attachment.id]


@pytest.mark.asyncio
async def test_generic_files_remain_references_without_authorized_model_support():

    async def read_content(attachment):
        pytest.fail("unsupported file must not be read")

    attachment = Attachment(
        id="file", name="archive.zip", mime_type="application/zip", size_bytes=4
    )
    response = await _project(
        AttachmentSupport(read_content=read_content),
        ModelRequest(
            model=FakeListChatModel(responses=["done"], profile={"image_inputs": True}),
            messages=[HumanMessage(content=[attachment.content_block()])],
            tools=[],
        ),
        _capture_response,
    )
    assert isinstance(response, ModelResponse)
    assert "application/zip" in str(response.result[0].content)
    assert "file-reading tool" in str(response.result[0].content)


@pytest.mark.asyncio
async def test_attachment_limits_apply_across_formats_before_model_invocation():
    files = [
        Attachment(id="old", name="a.png", mime_type="image/png", size_bytes=4),
        Attachment(id="audio", name="b.wav", mime_type="audio/wav", size_bytes=4),
        Attachment(id="video", name="c.mp4", mime_type="video/mp4", size_bytes=4),
    ]
    message = HumanMessage(
        content=[item.content_block() for item in [*files, files[-1]]]
    )
    reads = []

    async def read_content(attachment):
        reads.append(attachment.id)
        return AttachmentContent(data=b"test", mime_type=attachment.mime_type)

    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]), messages=[message], tools=[]
    )
    for byte_limit in (8, 7):
        support = AttachmentSupport(
            read_content=read_content,
            supports_content=lambda model, mime: True,
            max_attachments=2,
            max_bytes=byte_limit,
        )
        if byte_limit == 8:
            await _project(support, request, _capture_response)
        else:
            with pytest.raises(ValueError, match="max_bytes"):
                await _project(support, request, _capture_response)
        assert reads[-2:] == ["audio", "video"]
        assert message.content == [item.content_block() for item in [*files, files[-1]]]
    assert len(reads) == 4


@pytest.mark.asyncio
async def test_returned_rendition_requires_actual_model_capability():

    async def read_content(attachment):
        return AttachmentContent(data=b"test", mime_type="video/mp4")

    support = AttachmentSupport(read_content=read_content)
    with pytest.raises(ValueError, match="MIME type is not supported"):
        await _project(
            support,
            ModelRequest(
                model=FakeListChatModel(
                    responses=["done"], profile={"image_inputs": True}
                ),
                messages=[HumanMessage(content=[FILE.content_block()])],
                tools=[],
            ),
            _capture_response,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("remove", [False, True])
async def test_attachment_routing_respects_current_request_content(direct, remove):
    """Routing resolves preserved references without reviving removed content."""
    initial = _CaptureModel(
        responses=[AIMessage(content="must not run")], profile={"image_inputs": False}
    )
    destination = _CaptureModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": True}
    )
    reads = []
    checked_models = []

    async def read_content(attachment):
        reads.append(attachment.id)
        return AttachmentContent(data=b"test", mime_type="image/png")

    def supports_content(model, mime_type):
        checked_models.append(model)
        return model is destination and mime_type == "image/png"

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            messages = request.messages
            if remove:
                messages = [
                    message.model_copy(update={"content": "Attachment removed"})
                    for message in messages
                ]
            return await handler(request.override(model=destination, messages=messages))

    runtime = (
        TinkerFin()
        .with_namespace("attachments")
        .with_attachments(
            AttachmentSupport(
                read_content=read_content, supports_content=supports_content
            )
        )
        .build(model=initial, middleware=[Route()])
    )
    original = HumanMessage(content=[FILE.content_block()])
    if direct:
        graph = await create_graph(runtime)
        result = await graph.ainvoke({"messages": [original]})
    else:
        result = await runtime.ainvoke(
            thread_id="thread", run_id="run", input={"messages": [original]}
        )
    assert not initial.seen
    assert reads == ([] if remove else [FILE.id])
    assert ("data:image/png;base64,dGVzdA==" in str(destination.seen)) is not remove
    assert all(model is destination for model in checked_models)
    assert bool(checked_models) is not remove
    assert original.content == [FILE.content_block()]
    assert "data:image" not in str(result)
    assert "tinkerfin_attachment" not in str(result)
