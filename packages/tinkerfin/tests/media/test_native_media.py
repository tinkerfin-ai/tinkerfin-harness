"""Native media is projected for each model request without rewriting history."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import httpx
import pytest
from deepagents.backends import StateBackend
from deepagents.backends.protocol import FileData, ReadResult
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware, InputAgentState
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from openai import BadRequestError
from pydantic import Field, SecretStr
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport
from tinkerfin_tracing import Tracer

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PAYLOAD = "bmF0aXZlLW1lZGlh"
IMAGE = {"type": "image", "base64": PAYLOAD, "mime_type": "image/png"}
DOCUMENT = {"type": "file", "base64": PAYLOAD, "mime_type": PPTX}


class WorkspaceInput(InputAgentState):
    """Supply the file channel exposed by the configured state backend."""

    files: dict[str, FileData]


def messages_from(result: Mapping[str, object]) -> list[BaseMessage]:
    messages = result["messages"]
    assert isinstance(messages, list)
    assert all(isinstance(message, BaseMessage) for message in messages)
    return cast(list[BaseMessage], messages)


class RecordingModel(FakeMessagesListChatModel):
    requests: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[BaseTool | dict[str, Any] | type | Callable[..., Any]],
        **kwargs: Any,
    ):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        response = self.responses[self.i]
        self.i = (self.i + 1) % len(self.responses)
        return ChatResult(generations=[ChatGeneration(message=response)])


class RecordingOpenAI(ChatOpenAI):
    requests: list[list[BaseMessage]] = Field(default_factory=list)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


class BinaryBackend(StateBackend):
    async def aread(self, file_path, offset=0, limit=100):
        return ReadResult(file_data={"content": PAYLOAD, "encoding": "base64"})


@pytest.mark.asyncio
async def test_default_projection_rejects_non_pdf_on_an_openai_compatible_adapter():
    model = RecordingOpenAI(
        model="custom-document-model",
        api_key=SecretStr("unused"),
        base_url="https://example.invalid/v1",
        client=object(),
        async_client=object(),
        disable_streaming=True,
        profile={"image_inputs": False, "pdf_inputs": False},
    )
    system = SystemMessage(
        content=[{"type": "text", "text": "Inspect the file"}, DOCUMENT]
    )
    history = [
        HumanMessage(content=[DOCUMENT], id="user"),
        AIMessage(
            content=[DOCUMENT],
            id="assistant",
            tool_calls=[{"id": "read", "name": "read_file", "args": {}}],
        ),
        ToolMessage(
            content=[{"type": "text", "text": "File read succeeded"}, DOCUMENT],
            tool_call_id="read",
            status="success",
            additional_kwargs={"read_file_path": "/report.pptx"},
        ),
    ]
    graph = await create_graph(
        TinkerFin().with_namespace("media").build(model, system_prompt=system)
    )
    result = await graph.ainvoke({"messages": history})
    request = model.requests[-1]
    assert PAYLOAD not in str(request)
    assert "File read succeeded" in str(request)
    assert "/report.pptx" in str(request)
    tool = next(message for message in request if isinstance(message, ToolMessage))
    assert tool.tool_call_id == "read"
    assert tool.status == "success"
    assert PAYLOAD in str(result["messages"])
    assert system.content[-1] == DOCUMENT
    assert all(message.content[-1] == DOCUMENT for message in history)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_images_follow_the_routed_model_and_not_the_initial_profile(
    enabled,
):
    initial = RecordingModel(
        responses=[AIMessage(content="must not run")],
        profile={"image_inputs": not enabled},
    )
    destination = RecordingModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": enabled}
    )
    routed_content = []

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            routed_content.extend(request.messages)
            return await handler(request.override(model=destination))

    source = HumanMessage(content=[IMAGE])
    graph = await create_graph(
        TinkerFin().with_namespace("media").build(initial, middleware=[Route()])
    )
    result = await graph.ainvoke({"messages": [source]})
    assert not initial.requests
    assert PAYLOAD in str(routed_content)
    assert "tinkerfin_native_media" not in str(routed_content)
    assert (PAYLOAD in str(destination.requests[-1])) is enabled
    assert source.content == [IMAGE]
    assert PAYLOAD in str(result["messages"])


@pytest.mark.asyncio
async def test_explicit_native_file_support_does_not_require_an_attachment_reader():
    model = RecordingModel(responses=[AIMessage(content="done")])
    support = AttachmentSupport(
        supports_content=lambda candidate, mime: candidate is model and mime == PPTX
    )
    graph = await create_graph(
        TinkerFin().with_namespace("media").with_attachments(support).build(model)
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content=[DOCUMENT])]})
    assert PAYLOAD in str(model.requests[-1])
    assert messages_from(result)[-1].content == "done"


@pytest.mark.asyncio
async def test_attachment_without_reader_stays_available_as_a_reference():
    model = RecordingModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": True}
    )
    attachment = Attachment(
        id="upload", name="chart.png", mime_type="image/png", size_bytes=10
    )
    graph = await create_graph(TinkerFin().with_namespace("media").build(model))
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=[attachment.content_block()])]}
    )
    assert "no authorized attachment reader" in str(model.requests[-1])
    assert messages_from(result)[0].content == [attachment.content_block()]


@pytest.mark.asyncio
async def test_read_file_media_returns_a_usable_tool_result_to_a_text_model():
    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "read",
                        "name": "read_file",
                        "args": {"file_path": "/report.pptx"},
                    }
                ],
            ),
            AIMessage(
                content="The file remains available for a document-processing tool."
            ),
        ],
        profile={"image_inputs": False, "pdf_inputs": False},
    )
    graph = await create_graph(
        TinkerFin().with_namespace("media").build(model, backend=BinaryBackend())
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Read /report.pptx")]}
    )
    assert len(model.requests) == 2
    tool = next(
        message for message in model.requests[-1] if isinstance(message, ToolMessage)
    )
    assert "direct input" in str(tool.content)
    assert "/report.pptx" in str(tool.content)
    assert tool.status == "success"
    original = next(
        message for message in messages_from(result) if isinstance(message, ToolMessage)
    )
    assert PAYLOAD in str(original.content)
    assert str(messages_from(result)[-1].content).startswith(
        "The file remains available"
    )


@pytest.mark.asyncio
async def test_supported_tool_and_system_media_use_user_input_after_the_complete_batch():
    model = RecordingModel(
        responses=[AIMessage(content="done")],
        profile={"image_inputs": True, "image_tool_message": False},
    )
    system = SystemMessage(
        content=[{"type": "text", "text": "Inspect both results"}, IMAGE]
    )
    history = [
        AIMessage(
            content=[IMAGE],
            tool_calls=[
                {"id": "first", "name": "one", "args": {}},
                {"id": "second", "name": "two", "args": {}},
            ],
        ),
        ToolMessage(content=[IMAGE], tool_call_id="first"),
        ToolMessage(content=[IMAGE], tool_call_id="second"),
    ]
    graph = await create_graph(
        TinkerFin().with_namespace("media").build(model, system_prompt=system)
    )
    await graph.ainvoke({"messages": history})
    request = model.requests[-1]
    assert [message.type for message in request] == [
        "system",
        "ai",
        "tool",
        "tool",
        "human",
    ]
    assert [
        message.tool_call_id for message in request if isinstance(message, ToolMessage)
    ] == ["first", "second"]
    assert all(PAYLOAD not in str(message.content) for message in request[:-1])
    assert (
        sum(
            block.get("base64") == PAYLOAD
            for block in request[-1].content
            if isinstance(block, dict)
        )
        == 4
    )
    assert system.content[-1] == IMAGE


@pytest.mark.asyncio
async def test_checkpoint_media_survives_switching_between_supported_and_text_models():
    vision = RecordingModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": True}
    )
    text = RecordingModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": False}
    )

    class Route(AgentMiddleware):
        target = vision

        async def awrap_model_call(self, request, handler):
            return await handler(request.override(model=self.target))

    route = Route()
    graph = await create_graph(
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("media")
        .build(vision, middleware=[route])
    )
    config: RunnableConfig = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke(
        {"messages": [HumanMessage(content=[IMAGE], id="original")]}, config
    )
    route.target = text
    await graph.ainvoke(
        {"messages": [HumanMessage(content="Continue with text")]}, config
    )
    assert PAYLOAD not in str(text.requests[-1])
    route.target = vision
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Inspect the image again")]}, config
    )
    assert PAYLOAD in str(vision.requests[-1])
    assert messages_from(result)[0].content == [IMAGE]
    assert "tinkerfin_native_media" not in str(result)


@pytest.mark.asyncio
async def test_file_eviction_restores_native_media_before_downstream_hooks_and_checkpoint():
    model = RecordingModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": True}
    )
    backend = StateBackend()
    graph = await create_graph(
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("media")
        .build(
            model,
            backend=backend,
            middleware=[
                FilesystemMiddleware(
                    backend=backend, human_message_token_limit_before_evict=1
                )
            ],
        )
    )
    config: RunnableConfig = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=[{"type": "text", "text": "long input to offload"}, IMAGE],
                    id="original",
                )
            ]
        },
        config,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Continue")]}, config
    )
    assert PAYLOAD in str(model.requests[-1])
    assert messages_from(result)[0].content[-1] == IMAGE
    assert messages_from(result)[0].additional_kwargs.get("lc_evicted_to")
    assert "tinkerfin_native_media" not in str(result)
    assert "tinkerfin_native_media" not in str(model.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    ("block", "profile", "expected_mime"),
    [
        (
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{PAYLOAD}"},
            },
            {"image_inputs": True},
            "image/png",
        ),
        (
            {"type": "input_image", "image_url": f"data:image/png;base64,{PAYLOAD}"},
            {"image_inputs": True},
            "image/png",
        ),
        (
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": PAYLOAD,
                },
            },
            {"image_inputs": True},
            "image/png",
        ),
        (
            {"type": "input_audio", "input_audio": {"data": PAYLOAD, "format": "wav"}},
            {"audio_inputs": True},
            "audio/wav",
        ),
        (
            {"type": "video", "base64": PAYLOAD, "mime_type": "video/mp4"},
            {"video_inputs": True},
            "video/mp4",
        ),
        (
            {
                "type": "file",
                "file": {
                    "file_data": f"data:application/pdf;base64,{PAYLOAD}",
                    "filename": "report.pdf",
                },
            },
            {"pdf_inputs": True},
            "application/pdf",
        ),
        (
            {
                "type": "file",
                "file": {
                    "file_data": f"data:{PPTX};base64,{PAYLOAD}",
                    "filename": "report.pptx",
                },
            },
            {"pdf_inputs": True},
            None,
        ),
        (
            {
                "type": "input_file",
                "file_data": f"data:{PPTX};base64,{PAYLOAD}",
                "filename": "report.pptx",
            },
            {"pdf_inputs": True},
            None,
        ),
        (
            {"type": "file", "file": {"file_id": "opaque-file"}},
            {"pdf_inputs": True},
            None,
        ),
        (IMAGE, {}, None),
    ],
)
async def test_native_input_forms_keep_the_source_format(
    block, profile, expected_mime, wrapped
):
    if wrapped:
        block = {"type": "non_standard", "value": block}
    model = RecordingModel(responses=[AIMessage(content="done")], profile=profile)
    graph = await create_graph(TinkerFin().with_namespace("media").build(model))
    original = HumanMessage(content=[block])
    await graph.ainvoke({"messages": [original]})
    media = [
        item
        for message in model.requests[-1]
        for item in message.content_blocks
        if item["type"] in {"image", "audio", "video", "file"}
    ]
    if expected_mime is None:
        assert not media
        assert "direct input" in str(model.requests[-1])
    else:
        assert len(media) == 1
        assert media[0].get("mime_type") == expected_mime
        assert media[0].get("base64") == PAYLOAD
    assert original.content == [block]


@pytest.mark.asyncio
async def test_native_media_reaches_summary_as_a_file_reference_without_temporary_markers():
    model = RecordingModel(responses=[AIMessage(content="done")])
    summary = RecordingModel(responses=[AIMessage(content="An image was provided.")])
    backend = StateBackend()
    graph = await create_graph(
        TinkerFin()
        .with_namespace("media")
        .build(
            model,
            backend=backend,
            middleware=[
                SummarizationMiddleware(
                    summary,
                    backend=backend,
                    trigger=("messages", 2),
                    keep=("messages", 1),
                )
            ],
        )
    )
    original = HumanMessage(content=[IMAGE], id="image")
    result = await graph.ainvoke(
        {
            "messages": [
                original,
                AIMessage(content="ack"),
                HumanMessage(content="continue"),
            ]
        }
    )
    assert len(summary.requests) == 1
    assert "conversation_history/media/" in str(summary.requests[0])
    assert PAYLOAD not in str(summary.requests[0])
    assert "tinkerfin_native_media" not in str(summary.requests)
    assert "tinkerfin_native_media" not in str(result)
    assert original.content == [IMAGE]


@pytest.mark.asyncio
async def test_provider_rejection_propagates_without_retry_or_history_deletion():
    error = BadRequestError(
        "Provider rejected the request",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://example.invalid/v1")
        ),
        body={"error": {"code": "invalid_input"}},
    )

    class RejectedModel(RecordingModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.requests.append(messages)
            raise error

    model = RejectedModel(
        responses=[AIMessage(content="must not succeed")],
        profile={"image_inputs": True},
    )
    graph = await create_graph(TinkerFin().with_namespace("media").build(model))
    original = HumanMessage(content=[IMAGE])
    with pytest.raises(BadRequestError) as rejected:
        await graph.ainvoke({"messages": [original]})
    assert rejected.value is error
    assert len(model.requests) == 1
    assert original.content == [IMAGE]


@pytest.mark.asyncio
async def test_native_media_trace_does_not_capture_temporary_filesystem_markers():
    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "read",
                        "name": "read_file",
                        "args": {"file_path": "/image.png"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ],
        profile={"image_inputs": True},
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("media")
        .with_observer(tracer)
        .build(model, backend=BinaryBackend())
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Inspect /image.png")]},
    )
    history = await runtime.agui.history(tracer).get("thread")
    assert "tinkerfin_native_media" not in str(history)
    assert "tinkerfin_attachment_caption" not in str(history)
    assert history.snapshot.graph.nodes


@pytest.mark.parametrize(
    "configuration", ["workspace", "skills", "memory", "compaction"]
)
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("mime_type", [PPTX, "application/pdf"])
async def test_system_file_format_survives_framework_instruction_preparation(
    configuration, mime_type, wrapped
):
    initial = RecordingModel(
        responses=[AIMessage(content="must not run")], profile={"pdf_inputs": False}
    )
    destination = RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": True}
    )
    backend = StateBackend()
    block = {
        "type": "file",
        "file": {
            "file_data": f"data:{mime_type};base64,{PAYLOAD}",
            "filename": "report",
        },
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    if wrapped:
        block = {"type": "non_standard", "value": block}
    system = SystemMessage(content=[block])
    routed = []

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            routed.append(request.system_message)
            return await handler(request.override(model=destination))

    middleware: list[AgentMiddleware] = []
    if configuration == "compaction":
        middleware.append(
            SummarizationToolMiddleware(
                SummarizationMiddleware(initial, backend=backend),
                system_prompt="Compact completed research.",
            )
        )
    middleware.append(Route())
    runtime = (
        TinkerFin()
        .with_namespace("media")
        .build(
            initial,
            system_prompt=system,
            backend=_Workspace(backend) if configuration == "workspace" else backend,
            skills=["/skills/"] if configuration == "skills" else None,
            memory=["/context.md"] if configuration == "memory" else None,
            middleware=middleware,
        )
    )
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input=WorkspaceInput(
            messages=[HumanMessage(content="Inspect the supplied file")],
            files={
                "/skills/audit/SKILL.md": create_file_data(
                    "---\nname: audit\ndescription: Inspect audit facts\n---\nUse observed evidence."
                ),
                "/context.md": create_file_data(
                    "Use the customer's accounting definitions."
                ),
            },
        ),
    )
    assert not initial.requests
    assert system.content == [block]
    assert routed[0] is not None and block in routed[0].content
    assert "tinkerfin_native_media" not in str(routed)
    assert (PAYLOAD in str(destination.requests[-1])) is (
        mime_type == "application/pdf"
    )
    assert "tinkerfin_native_media" not in str(result)
    if configuration == "skills":
        assert "Inspect audit facts" in str(destination.requests[-1])
    elif configuration == "memory":
        assert "accounting definitions" in str(destination.requests[-1])


async def test_system_media_removed_by_routing_is_not_restored_after_memory():
    backend = StateBackend()
    model = RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": True}
    )
    block = {
        "type": "file",
        "file": {
            "file_data": f"data:application/pdf;base64,{PAYLOAD}",
            "filename": "report.pdf",
        },
    }
    system = SystemMessage(content=[block])

    class RemoveMedia(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            assert request.system_message is not None
            assert isinstance(request.system_message.content, list)
            assert block in request.system_message.content
            return await handler(
                request.override(
                    system_message=SystemMessage(content="The file was removed.")
                )
            )

    runtime = (
        TinkerFin()
        .with_namespace("media")
        .build(
            model,
            system_prompt=system,
            backend=backend,
            middleware=[MemoryMiddleware(backend=backend, sources=[]), RemoveMedia()],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert "The file was removed" in str(model.requests[-1])
    assert PAYLOAD not in str(model.requests[-1])
    assert system.content == [block]


@pytest.mark.parametrize("existing_cache", [False, True])
async def test_memory_cache_breakpoint_survives_system_media_preparation(
    existing_cache,
):
    initial = ChatAnthropic(
        model_name="claude-sonnet-4-6",
        api_key=SecretStr("unused"),
        profile={"pdf_inputs": True},
        timeout=None,
        stop=None,
    )
    destination = RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": True}
    )
    block: dict[str, Any] = {
        "type": "file",
        "base64": PAYLOAD,
        "mime_type": "application/pdf",
    }
    if existing_cache:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    system = SystemMessage(content=[block])
    routed = []

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            routed.append(request.system_message)
            return await handler(request.override(model=destination))

    backend = StateBackend()
    runtime = (
        TinkerFin()
        .with_namespace("media")
        .build(
            initial,
            system_prompt=system,
            backend=backend,
            middleware=[
                MemoryMiddleware(
                    backend=backend,
                    sources=[],
                    system_prompt=None,
                    add_cache_control=True,
                ),
                Route(),
            ],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Inspect")]},
    )
    assert routed[0] is not None
    assert routed[0].content == [{**block, "cache_control": {"type": "ephemeral"}}]
    projected = [
        item
        for message in destination.requests[-1]
        for item in message.content_blocks
        if item["type"] == "file"
    ]
    assert len(projected) == 1 and projected[0].get("cache_control") == {
        "type": "ephemeral"
    }
    assert system.content == [block]


@pytest.mark.parametrize("reader_mode", ["missing", "allowed", "denied"])
async def test_wrapped_attachment_requires_the_authorized_reader(reader_mode):
    attachment = Attachment(
        id="private-file", name="report.pdf", mime_type="application/pdf", size_bytes=12
    )
    block = {"type": "non_standard", "value": attachment.content_block()}
    original = HumanMessage(content=[block])
    model = RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": True}
    )
    reads = []
    failure = PermissionError("The attachment belongs to another account")

    async def read_content(value):
        reads.append(value)
        if reader_mode == "denied":
            raise failure
        return AttachmentContent(data=b"native-media", mime_type="application/pdf")

    support = AttachmentSupport(
        read_content=None if reader_mode == "missing" else read_content
    )
    runtime = TinkerFin().with_namespace("media").with_attachments(support).build(model)
    if reader_mode == "denied":
        with pytest.raises(PermissionError) as rejected:
            await runtime.ainvoke(
                thread_id="thread", run_id="run", input={"messages": [original]}
            )
        assert rejected.value is failure
        assert not model.requests
    else:
        result = await runtime.ainvoke(
            thread_id="thread", run_id="run", input={"messages": [original]}
        )
        assert (PAYLOAD in str(model.requests[-1])) is (reader_mode == "allowed")
        if reader_mode == "missing":
            assert "no authorized attachment reader" in str(model.requests[-1])
        assert messages_from(result)[0].content == [block]
    assert reads == ([] if reader_mode == "missing" else [attachment])
    assert original.content == [block]


async def test_unknown_domain_blocks_are_not_scanned_for_nested_media():
    blocks: list[str | dict[str, Any]] = [
        {"type": "invoice", "value": DOCUMENT},
        {"type": "non_standard", "value": {"type": "invoice", "evidence": DOCUMENT}},
    ]
    model = RecordingModel(
        responses=[AIMessage(content="done")], profile={"pdf_inputs": False}
    )
    graph = await create_graph(TinkerFin().with_namespace("media").build(model))
    await graph.ainvoke({"messages": [HumanMessage(content=blocks)]})
    assert model.requests[-1][0].content == blocks
