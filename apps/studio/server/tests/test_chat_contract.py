from typing import cast

import pytest
from ag_ui.core import RunAgentInput
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.conversation_router import chat
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.request import MAX_USER_MESSAGE_BYTES, ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    prepare_run_request,
)
from tinkerfin_studio.conversation.service import parse_last_event_id


def test_chat_request_preserves_command_extensions_and_derives_plan_mode() -> None:
    """command 扩展应完整保留，plan 状态只在运行准备边界解释"""

    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [
                    {"id": "client-request-1", "role": "user", "content": "执行任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {
                    "model": "main",
                    "command": {"plan": "on", "compact": "保留这段命令输入"},
                    "trace": "x",
                },
            }
        )
    )

    normalized = request.normalized_json(
        thread_id="thread-1",
        message_ids=("message-server-1",),
    )

    payload = normalized
    assert payload["threadId"] == "thread-1"
    assert payload["messages"] == [
        {
            "id": "message-server-1",
            "role": "user",
            "content": "执行任务",
            "name": None,
            "encryptedValue": None,
        }
    ]
    assert payload["forwardedProps"] == {
        "accessMode": "full",
        "model": "main",
        "command": {"plan": "on", "compact": "保留这段命令输入"},
        "trace": "x",
    }
    prepared = prepare_run_request(request, user_id=7, thread_id="thread-1")
    assert prepared.mode == "plan"


def test_chat_request_accepts_a_multi_segment_colon_thread_id() -> None:
    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "tenant:workspace:conversation:thread-1",
                "runId": "run-colon-thread",
                "state": {},
                "messages": [
                    {"id": "client-colon", "role": "user", "content": "继续任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {
                    "model": "main",
                    "command": {"plan": "off"},
                },
            }
        )
    )

    assert request.thread_id == "tenant:workspace:conversation:thread-1"


@pytest.mark.parametrize(
    "forwarded_props",
    (
        {"model": "main", "mode": "plan"},
        {"model": "main", "command": {"plan": "invalid"}},
        {"model": "main", "command": {}},
    ),
)
def test_chat_request_rejects_removed_or_invalid_plan_commands(
    forwarded_props: dict[str, object],
) -> None:
    """当前请求必须只使用精确的 command.plan 契约"""

    with pytest.raises(ValidationError):
        ChatRequest.from_agui(
            RunAgentInput.model_validate(
                {
                    "threadId": "",
                    "runId": "run-invalid-command",
                    "state": {},
                    "messages": [],
                    "tools": [],
                    "context": [],
                    "forwardedProps": forwarded_props,
                }
            )
        )


async def test_chat_route_maps_studio_secondary_validation_to_safe_422() -> None:
    protocol_input = RunAgentInput.model_validate(
        {
            "threadId": "",
            "runId": "run-invalid-studio-contract",
            "state": {},
            "messages": [
                {"id": "client-invalid", "role": "user", "content": "执行任务"}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "command": {}},
        }
    )

    with pytest.raises(RequestValidationError) as caught:
        await chat(
            input_data=protocol_input,
            request=cast(Request, object()),
            session=cast(AsyncSession, object()),
            user=cast(UserContext, object()),
            last_event_id=None,
        )

    assert caught.value.errors()
    assert all("input" not in error for error in caught.value.errors())


def test_chat_request_drops_the_protocol_message_id() -> None:
    """HTTP 要求客户端 ID，但业务快照只使用服务端权威 ID"""

    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [
                    {"id": "client-message-1", "role": "user", "content": "执行任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )
    )

    assert request.messages == [
        {"role": "user", "content": "执行任务", "name": None, "encryptedValue": None}
    ]
    normalized = request.normalized_json(
        thread_id="thread-1",
        message_ids=("message-server-1",),
    )
    normalized_model = RunAgentInput.model_validate(normalized)
    assert normalized_model.messages[0].id == "message-server-1"


def test_chat_request_enforces_the_utf8_user_message_capacity_before_side_effects() -> (
    None
):
    def protocol_input(content: str) -> RunAgentInput:
        return RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-capacity",
                "state": {},
                "messages": [
                    {"id": "client-capacity", "role": "user", "content": content}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )

    accepted = ChatRequest.from_agui(protocol_input("a" * MAX_USER_MESSAGE_BYTES))
    assert len(str(accepted.messages[0]["content"]).encode()) == MAX_USER_MESSAGE_BYTES
    multibyte = ChatRequest.from_agui(
        protocol_input("你" * (MAX_USER_MESSAGE_BYTES // 3))
    )
    assert len(str(multibyte.messages[0]["content"]).encode()) <= MAX_USER_MESSAGE_BYTES

    with pytest.raises(BusinessException) as caught:
        ChatRequest.from_agui(protocol_input("a" * (MAX_USER_MESSAGE_BYTES + 1)))
    assert caught.value.error_code is ConversationErrorCode.REQUEST_TOO_LARGE


@pytest.mark.parametrize("content", ["\ud800", "\udfff"])
async def test_chat_rejects_an_unpaired_unicode_surrogate_as_invalid_input(
    content: str,
) -> None:
    """不能编码为 UTF-8 的 JSON 文本必须在业务副作用前返回校验失败"""

    protocol_input = RunAgentInput.model_validate(
        {
            "threadId": "",
            "runId": "run-invalid-unicode",
            "state": {},
            "messages": [
                {"id": "client-invalid-unicode", "role": "user", "content": content}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "command": {"plan": "off"}},
        }
    )

    with pytest.raises(RequestValidationError) as caught:
        await chat(
            input_data=protocol_input,
            request=cast(Request, object()),
            session=cast(AsyncSession, object()),
            user=cast(UserContext, object()),
            last_event_id=None,
        )

    assert any("有效 UTF-8" in str(error["ctx"]) for error in caught.value.errors())


def test_resume_request_rejects_an_unexecuted_user_message() -> None:
    with pytest.raises(ValidationError, match="恢复运行不得同时提交新消息"):
        ChatRequest.model_validate(
            {
                "threadId": "thread-1",
                "runId": "run-resume-with-message",
                "state": {},
                "messages": [{"role": "user", "content": "不应执行"}],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
                "resume": [
                    {
                        "interruptId": "interrupt-1",
                        "status": "cancelled",
                    }
                ],
            }
        )


def test_chat_request_rejects_self_referential_parent_run() -> None:
    """parentRunId 必须选择已有分支点，不能引用当前 run"""

    with pytest.raises(ValidationError, match="parentRunId"):
        ChatRequest.model_validate(
            {
                "threadId": "thread-1",
                "runId": "run-1",
                "parentRunId": "run-1",
                "state": {},
                "messages": [],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )


def test_from_agui_rejects_full_history_in_a_single_increment_request() -> None:
    """普通请求只提交一条增量消息，不能把完整历史重复作为新输入"""

    protocol_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-roles",
            "parentRunId": "run-parent",
            "state": {"draft": True},
            "messages": [
                {"id": "developer-1", "role": "developer", "content": "规则"},
                {"id": "system-1", "role": "system", "content": "系统"},
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "调用工具",
                    "toolCalls": [
                        {
                            "id": "call-1",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "id": "user-1",
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析附件"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "value": "https://example.test/chart.png",
                                "mimeType": "image/png",
                            },
                            "metadata": {"alt": "图表"},
                        },
                        {
                            "type": "document",
                            "source": {
                                "type": "data",
                                "value": "cGRm",
                                "mimeType": "application/pdf",
                            },
                        },
                    ],
                },
                {
                    "id": "tool-1",
                    "role": "tool",
                    "toolCallId": "call-1",
                    "content": "完成",
                },
                {
                    "id": "activity-1",
                    "role": "activity",
                    "activityType": "progress",
                    "content": {"percent": 50},
                },
                {"id": "reasoning-1", "role": "reasoning", "content": "思考"},
            ],
            "tools": [
                {
                    "name": "client_tool",
                    "description": "客户端声明",
                    "parameters": {"type": "object"},
                    "vendor": "kept",
                }
            ],
            "context": [{"description": "tenant", "value": "acme", "vendor": "kept"}],
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "off"},
                "trace": {"sampled": True},
            },
        }
    )

    with pytest.raises(ValidationError, match="一条 user 消息"):
        ChatRequest.from_agui(protocol_input)


def test_image_input_rejects_external_urls_instead_of_stored_references() -> None:
    with pytest.raises(ValidationError, match="附件引用不合法"):
        ChatRequest.from_agui(
            RunAgentInput.model_validate(
                {
                    "threadId": "",
                    "runId": "run-multimodal",
                    "state": {},
                    "messages": [
                        {
                            "id": "user-1",
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "分析附件"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "url",
                                        "value": "https://example.test/chart.png",
                                        "mimeType": "image/png",
                                    },
                                },
                            ],
                        },
                    ],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {
                        "model": "main",
                        "command": {"plan": "off"},
                    },
                }
            )
        )


def test_client_message_id_does_not_change_the_canonical_business_snapshot() -> None:
    def request(client_id: str) -> ChatRequest:
        return ChatRequest.from_agui(
            RunAgentInput.model_validate(
                {
                    "threadId": "thread-1",
                    "runId": "run-1",
                    "state": {},
                    "messages": [
                        {"id": client_id, "role": "user", "content": "同一请求"}
                    ],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {"model": "main", "command": {"plan": "off"}},
                }
            )
        )

    first = prepare_run_request(
        request("client-a"),
        user_id=7,
        thread_id="thread-1",
    )
    second = prepare_run_request(
        request("client-b"),
        user_id=7,
        thread_id="thread-1",
    )

    assert first.input_json == second.input_json
    assert first.message_ids == second.message_ids


@pytest.mark.parametrize("run_id", [" run-1", "run-1 ", "   "])
def test_chat_request_rejects_noncanonical_run_id(run_id: str) -> None:
    """runId 必须在任何持久化或事件源创建前拒绝首尾空白"""

    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "threadId": "",
                "runId": run_id,
                "state": {},
                "messages": [{"role": "user", "content": "执行任务"}],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )


@pytest.mark.parametrize("value", ["-1", "01", "1.0", " 1", "1 ", "一"])
def test_last_event_id_rejects_noncanonical_values(value: str) -> None:
    """非法游标必须在发送 SSE 响应头前失败"""

    with pytest.raises(BusinessException) as caught:
        parse_last_event_id(value)

    assert int(caught.value.error_code) == 1_001_004_019


def test_conversation_routes_are_registered_with_the_locked_paths() -> None:
    """应用 OpenAPI 应暴露固定的会话查询与运行接口"""

    paths = create_application(lifespan=None).openapi()["paths"]

    assert "/api/conversation/chat" in paths
    assert "/api/conversation/history" in paths
    assert "/api/conversation/config" in paths
    assert "/api/conversation/{thread_id}/history" in paths
    assert "/api/conversation/{thread_id}/trace" in paths
    assert "/api/conversation/{thread_id}/runs/{run_id}/cancel" in paths
    history_parameters = paths["/api/conversation/history"]["get"]["parameters"]
    assert {
        parameter["name"]
        for parameter in history_parameters
        if parameter["in"] == "query"
    } == {
        "pageSize",
        "cursor",
        "query",
    }
    detail_parameters = paths["/api/conversation/{thread_id}/history"]["get"][
        "parameters"
    ]
    assert {
        parameter["name"]
        for parameter in detail_parameters
        if parameter["in"] == "query"
    } == {"historyCursor", "includeTaskTrace", "limit"}
    trace_parameters = paths["/api/conversation/{thread_id}/trace"]["get"]["parameters"]
    assert {
        parameter["name"]
        for parameter in trace_parameters
        if parameter["in"] == "query"
    } == {"includeTaskTrace"}
