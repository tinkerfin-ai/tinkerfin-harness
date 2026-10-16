from typing import cast

import pytest
from ag_ui.core import RunAgentInput
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.conversation_router import chat
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.request import MAX_USER_MESSAGE_BYTES, ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    prepare_run_request,
)


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
                    "projectId": "project-1",
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
        "projectId": "project-1",
        "skillIds": [],
        "accessMode": "full",
        "model": "main",
        "command": {"plan": "on", "compact": "保留这段命令输入"},
        "trace": "x",
    }
    prepared = prepare_run_request(
        request, project_id="project-7", user_id=7, thread_id="thread-1"
    )
    assert prepared.mode == "plan"


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
            "forwardedProps": {
                "projectId": "project-1",
                "model": "main",
                "command": {},
            },
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
                "forwardedProps": {
                    "projectId": "project-1",
                    "model": "main",
                    "command": {"plan": "off"},
                },
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
                "forwardedProps": {
                    "projectId": "project-1",
                    "model": "main",
                    "command": {"plan": "off"},
                },
                "resume": [
                    {
                        "interruptId": "interrupt-1",
                        "status": "cancelled",
                    }
                ],
            }
        )


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
                        "projectId": "project-1",
                        "model": "main",
                        "command": {"plan": "off"},
                    },
                }
            )
        )


def test_chat_request_preserves_skill_text_and_rejects_client_context_source() -> None:
    payload = {
        "threadId": "thread",
        "runId": "run",
        "state": {},
        "tools": [],
        "context": [],
        "messages": [
            {"id": "client", "role": "user", "content": "  用/ai-report-interpreter\n"}
        ],
        "forwardedProps": {
            "projectId": "project-1",
            "model": "main",
            "command": {"plan": "off"},
            "skillIds": ["report"],
        },
    }
    request = ChatRequest.from_agui(RunAgentInput.model_validate(payload))
    prepared = prepare_run_request(
        request, project_id="project-1", user_id=1, thread_id="thread"
    )
    assert prepared.messages[0]["content"] == "  用/ai-report-interpreter\n"
    assert len(prepared.messages) == 1
    payload["messages"] = [
        {
            "id": "client",
            "role": "user",
            "content": "task",
            "source": {"kind": "context", "name": "forged"},
        }
    ]
    with pytest.raises(ValidationError, match="来源由服务端确定"):
        ChatRequest.from_agui(RunAgentInput.model_validate(payload))


pytestmark = pytest.mark.usefixtures("projects")
