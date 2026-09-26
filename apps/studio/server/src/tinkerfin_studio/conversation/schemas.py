"""会话列表、Trace 历史、实时跟随和命令接口模型"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin.agui import (
    AgUiTraceGraphDelta,
    AgUiTraceInteraction,
    AgUiTraceMessage,
)
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.conversation.failures import ConversationRunFailure
from tinkerfin_studio.conversation.models import TitleGenerationStatus, TitleSource
from tinkerfin_studio.conversation.todo_groups import TaskTraceSnapshot
from tinkerfin_studio.conversation.trace_responses import (
    ConversationGraph,
    ConversationGraphQueryPage,
    ConversationTraceUpdate,
)
from tinkerfin_tracing import (
    TraceCompleteness,
    TraceReasoning,
    TraceState,
    TraceStatus,
)

PendingInteractionKind = Literal[
    "tool_approval",
    "plan_clarification",
    "plan_review",
    "input_required",
]


class ConversationTitle(BaseModel):
    """标题更新按独立序号合并，不受聊天流或历史响应先后影响"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    thread_id: str = Field(alias="threadId")
    title: str = Field(min_length=1, max_length=32)
    title_source: TitleSource = Field(default="default", alias="titleSource")
    title_generation_status: TitleGenerationStatus = Field(
        default="idle", alias="titleGenerationStatus"
    )
    title_seq: int = Field(
        default=0,
        ge=0,
        alias="titleSeq",
        description="标题及生成状态每次提交递增的序号",
    )


class ConversationHistoryListItem(ConversationTitle):
    """历史列表中的会话摘要"""

    id: int = Field(ge=1)
    status: str
    last_run_id: str | None = Field(default=None, alias="lastRunId")
    last_model: str | None = Field(default=None, alias="lastModel")
    access_mode: AccessMode = Field(default="full", alias="accessMode")
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    has_pending_interrupt: bool = Field(alias="hasPendingInterrupt")
    pending_interaction_kind: PendingInteractionKind | None = Field(
        alias="pendingInteractionKind",
        description="待处理交互的产品类型；无待处理交互时为 null",
    )
    pinned: bool
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="最近一次权威 Trace 活动时间",
    )


class ConversationHistoryListResponse(BaseModel):
    """历史会话游标分页响应"""

    model_config = ConfigDict(populate_by_name=True)

    items: list[ConversationHistoryListItem]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class ConversationHistoryGroupConfig(BaseModel):
    """历史会话时间分组配置"""

    model_config = ConfigDict(populate_by_name=True)

    day_ranges: list[int] = Field(
        alias="dayRanges",
        min_length=1,
        description="非今日会话使用的升序最大自然日差",
    )


class ConversationHistoryDetail(ConversationTitle):
    """用户归属校验后的固定前缀 Trace 会话视图"""

    id: int = Field(ge=1)
    last_model: str | None = Field(default=None, alias="lastModel")
    access_mode: AccessMode = Field(default="full", alias="accessMode")
    pinned: bool
    as_of_seq: int = Field(alias="asOfSeq", ge=1)
    generation: str
    observed_at: datetime = Field(
        alias="observedAt", description="Trace 存储的 UTC 观测时间"
    )
    head_run_id: str = Field(alias="headRunId")
    available_heads: tuple[str, ...] = Field(alias="availableHeads")
    history_cursor: str | None = Field(default=None, alias="historyCursor")
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    messages: tuple[AgUiTraceMessage, ...]
    run_failures: tuple[ConversationRunFailure, ...] = Field(alias="runFailures")
    reasoning: tuple[TraceReasoning, ...]
    graph: ConversationGraph
    state: TraceState
    interactions: tuple[AgUiTraceInteraction, ...]
    status: TraceStatus
    completeness: TraceCompleteness
    task_trace: TaskTraceSnapshot | None = Field(alias="taskTrace")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="最近一次权威 Trace 活动时间",
    )


class ConversationTraceSnapshotEvent(BaseModel):
    """Trace SSE 建立连接后首先发送的完整权威快照"""

    type: Literal["snapshot"] = "snapshot"
    snapshot: ConversationHistoryDetail


class ConversationRunSnapshotEvent(BaseModel):
    """已有运行续播前的历史基线，以及后续是否还有运行事件"""

    type: Literal["snapshot"] = "snapshot"
    snapshot: ConversationHistoryDetail
    replay: bool = Field(description="是否继续发送该运行已提交及后续产生的事件")


class ConversationTraceUpdateEvent(BaseModel):
    """Trace SSE 在快照之后发送的语义增量"""

    type: Literal["update"] = "update"
    update: ConversationTraceUpdate
    run_failures: tuple[ConversationRunFailure, ...] = Field(alias="runFailures")
    task_trace: TaskTraceSnapshot | None = Field(alias="taskTrace")


class ConversationTraceErrorEvent(BaseModel):
    """Trace SSE 已开始后可安全重试的终止信号"""

    type: Literal["error"] = "error"
    code: Literal["trace_unavailable"] = "trace_unavailable"


class ConversationTraceGraphSnapshotEvent(BaseModel):
    """链路跟随连接建立后的完整筛选页"""

    type: Literal["snapshot"] = "snapshot"
    snapshot: ConversationGraphQueryPage


class ConversationTraceGraphUpdateEvent(BaseModel):
    """同一筛选条件下的链路增删、完整顺序、直接命中与完整性更新"""

    type: Literal["update"] = "update"
    update: AgUiTraceGraphDelta


class ConversationTraceGraphErrorEvent(BaseModel):
    """链路跟随开始后可安全重试的终止信号"""

    type: Literal["error"] = "error"
    code: Literal["trace_unavailable"] = "trace_unavailable"


class ConversationThreadUpdate(BaseModel):
    """重命名或置顶请求"""

    title: str | None = Field(default=None, min_length=1, max_length=32)
    pinned: bool | None = None


class CancelRunResponse(BaseModel):
    """分布式取消结果"""

    cancelled: bool = Field(description="本请求是否发起并完成了取消")


__all__ = [
    "CancelRunResponse",
    "ConversationHistoryDetail",
    "ConversationHistoryGroupConfig",
    "ConversationHistoryListItem",
    "ConversationHistoryListResponse",
    "ConversationThreadUpdate",
    "ConversationTraceErrorEvent",
    "ConversationTraceGraphErrorEvent",
    "ConversationTraceGraphSnapshotEvent",
    "ConversationTraceGraphUpdateEvent",
    "ConversationTraceSnapshotEvent",
    "ConversationTraceUpdateEvent",
    "PendingInteractionKind",
]
