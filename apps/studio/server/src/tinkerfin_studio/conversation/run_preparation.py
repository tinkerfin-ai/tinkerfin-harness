"""会话请求意图、权威快照、恢复状态与主事件增强"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from ag_ui.core.types import ResumeEntry
from langchain_core.runnables import RunnableConfig
from pydantic import JsonValue

from tinkerfin import AgentMode, RunIdentity
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.models import TitleGenerationStatus, TitleSource
from tinkerfin_studio.conversation.request import ChatRequest, CompactRequest


def conversation_identity(thread_id: str, run_id: str, *, user_id: int) -> RunIdentity:
    """创建公开生命周期与持久执行共用的会话身份"""

    return RunIdentity(namespace=f"ns_{user_id}", thread_id=thread_id, run_id=run_id)


@dataclass(frozen=True, slots=True)
class StartChatIntent:
    """一次普通提问的会话标题与已授权附件"""

    attachments: tuple[Attachment, ...]
    title: str


@dataclass(frozen=True, slots=True)
class ResumeChatIntent:
    """一次完整覆盖当前审批批次的恢复请求"""

    entries: tuple[ResumeEntry, ...]


@dataclass(frozen=True, slots=True)
class CompactIntent:
    """整理已有会话的上下文，不提交新消息"""

    thread_id: str


ChatIntent = StartChatIntent | ResumeChatIntent | CompactIntent


@dataclass(frozen=True, slots=True)
class PreparedRunRequest:
    """数据库、Messaging、Agent 与主开始事件共用的权威请求事实"""

    input_json: dict[str, JsonValue]
    messages: tuple[dict[str, JsonValue], ...]
    identity: RunIdentity
    parent_run_id: str | None
    graph_config: RunnableConfig
    message_ids: tuple[str, ...]
    mode: AgentMode
    access_mode: AccessMode = "full"
    operation: Literal["chat", "compact"] = "chat"


@dataclass(frozen=True, slots=True)
class RegisteredRun:
    """已持久化或已确认附着的主 run"""

    run_id: int
    created: bool


def classify_intent(request: ChatRequest) -> ChatIntent:
    """只执行一次普通运行与恢复运行分类"""

    if request.resume is not None:
        if not request.thread_id.strip():
            raise BusinessException(ConversationErrorCode.RESUME_THREAD_ID_REQUIRED)
        if not request.resume:
            raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
        return ResumeChatIntent(entries=tuple(request.resume))

    submission = request.user_input
    return StartChatIntent(
        attachments=submission.attachments,
        title=submission.text.strip()[:16] or "附件提问",
    )


def prepare_run_request(
    request: ChatRequest | CompactRequest,
    *,
    user_id: int,
    thread_id: str,
    access_mode: AccessMode = "full",
) -> PreparedRunRequest:
    """分配服务端消息 ID 并构造一次权威标准请求快照"""

    if isinstance(request, CompactRequest):
        return PreparedRunRequest(
            input_json={
                "operation": "compact",
                "threadId": thread_id,
                **request.model_dump(mode="json", by_alias=True),
            },
            messages=(),
            identity=conversation_identity(thread_id, request.run_id, user_id=user_id),
            parent_run_id=None,
            graph_config={},
            message_ids=(),
            mode="default",
            access_mode=access_mode,
            operation="compact",
        )

    message_ids = tuple(
        "message-"
        + str(
            uuid5(
                NAMESPACE_URL,
                f"tinkerfin-studio:{user_id}:{thread_id}:{request.run_id}:{index}",
            )
        )
        for index in range(len(request.messages))
    )
    input_json = request.normalized_json(
        thread_id=thread_id,
        message_ids=message_ids,
    )
    identity = conversation_identity(thread_id, request.run_id, user_id=user_id)
    graph_config: RunnableConfig = {
        "configurable": {
            "forwarded_props": request.forwarded_props.model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    }
    return PreparedRunRequest(
        input_json=input_json,
        messages=tuple(cast(list[dict[str, JsonValue]], input_json["messages"])),
        identity=identity,
        parent_run_id=request.parent_run_id,
        graph_config=graph_config,
        message_ids=message_ids,
        mode=request.forwarded_props.agent_mode,
        access_mode=request.forwarded_props.access_mode,
    )


def decorate_main_event(
    event: BaseEvent,
    *,
    prepared: PreparedRunRequest,
    title: str,
    title_source: TitleSource = "default",
    title_generation_status: TitleGenerationStatus = "idle",
    title_seq: int = 0,
) -> BaseEvent:
    """只补充 Studio 标题和取消文案"""

    run_id = prepared.identity.run_id
    if isinstance(event, RunStartedEvent) and event.run_id == run_id:
        return event.model_copy(
            update={
                "title": title,
                "titleSource": title_source,
                "titleGenerationStatus": title_generation_status,
                "titleSeq": title_seq,
            }
        )
    if isinstance(event, RunErrorEvent):
        raw_event = event.raw_event
        if not isinstance(raw_event, dict):
            return event
        if raw_event.get("runId") != run_id:
            return event
        if event.code == "cancelled":
            return event.model_copy(
                update={
                    "message": "上下文压缩已停止"
                    if prepared.operation == "compact"
                    else "聊天生成已取消"
                }
            )
    return event


__all__ = [
    "ChatIntent",
    "CompactIntent",
    "PreparedRunRequest",
    "RegisteredRun",
    "ResumeChatIntent",
    "StartChatIntent",
    "classify_intent",
    "conversation_identity",
    "decorate_main_event",
    "prepare_run_request",
]
