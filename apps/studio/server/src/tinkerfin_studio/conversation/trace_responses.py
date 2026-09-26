"""对话展示与独立链路查询的 HTTP 数据边界"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from tinkerfin.agui import (
    AgUiSubagentReference,
    AgUiToolReference,
    AgUiTraceGraph,
    AgUiTraceGraphDelta,
    AgUiTraceGraphNode,
    AgUiTraceGraphPage,
    AgUiTraceInteraction,
    AgUiTraceMessage,
    AgUiTraceUpdate,
)
from tinkerfin_tracing import (
    TraceCompleteness,
    TraceEntityDelta,
    TraceGraphCompleteness,
    TraceGraphFailure,
    TraceGraphLinkIssue,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphTurn,
    TraceReasoning,
    TraceState,
    TraceStatus,
)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
        frozen=True,
    )


class _ConversationNode(_ResponseModel):
    """对话中的节点展示信息；模型请求正文由独立链路查询提供"""

    id: str
    turn_id: str
    parent_subagent_id: str | None
    context_kind: (
        Literal["memory", "guardrail", "retrieval", "custom", "compaction"] | None
    )
    compaction_origin: Literal["manual", "automatic", "tool"] | None
    parent_node_id: str | None
    model_call_id: str | None
    status: TraceGraphNodeStatus
    name: str
    run_id: str
    graph_namespace: tuple[str, ...]
    agent_name: str | None
    provider: str | None
    model: str | None
    source_id: str | None
    started_at: datetime
    first_output_at: datetime | None
    completed_at: datetime | None
    started_seq: int
    updated_seq: int
    content: JsonValue | None
    content_omitted: bool
    tool_call_only: bool
    request_omitted: bool = Field(description="采集或保留策略是否遗漏了请求正文")
    result: JsonValue | None
    result_omitted: bool
    usage: JsonValue | None
    response_metadata: JsonValue | None
    failure: TraceGraphFailure | None
    link_issues: tuple[TraceGraphLinkIssue, ...]
    agui: (
        Annotated[
            AgUiToolReference | AgUiSubagentReference, Field(discriminator="kind")
        ]
        | None
    )


class ConversationModelNode(_ConversationNode):
    """模型调用展示，不发送对话视图不使用的请求正文"""

    kind: Literal[TraceGraphNodeKind.MODEL]


class ConversationExecutionNode(_ConversationNode):
    """对话中的工具、子代理及其他节点，保留其请求和结果"""

    kind: Literal[
        TraceGraphNodeKind.HUMAN_MESSAGE,
        TraceGraphNodeKind.ASSISTANT_MESSAGE,
        TraceGraphNodeKind.CONTEXT,
        TraceGraphNodeKind.TOOL,
        TraceGraphNodeKind.SUBAGENT,
        TraceGraphNodeKind.MEMORY,
        TraceGraphNodeKind.GUARDRAIL,
        TraceGraphNodeKind.RETRIEVAL,
        TraceGraphNodeKind.CUSTOM,
        TraceGraphNodeKind.PLAN,
        TraceGraphNodeKind.INTERACTION,
    ]
    request: JsonValue | None


ConversationGraphNode = Annotated[
    ConversationModelNode | ConversationExecutionNode, Field(discriminator="kind")
]


def _conversation_node(node: AgUiTraceGraphNode) -> ConversationGraphNode:
    # 从已校验节点读取所需属性，避免先复制并编码随后要舍弃的模型请求
    if node.kind is TraceGraphNodeKind.MODEL:
        return ConversationModelNode.model_validate(node)
    return ConversationExecutionNode.model_validate(node)


class ConversationGraph(_ResponseModel):
    """对话展示所需的完整节点顺序与关联信息"""

    turns: tuple[TraceGraphTurn, ...]
    nodes: tuple[ConversationGraphNode, ...]
    ordered_node_ids: tuple[str, ...]
    matched_node_ids: tuple[str, ...]
    as_of_seq: int
    completeness: TraceGraphCompleteness

    @classmethod
    def from_graph(cls, graph: AgUiTraceGraph) -> "ConversationGraph":
        """保留对话节点关系与非模型请求正文"""
        return cls(
            turns=graph.turns,
            nodes=tuple(_conversation_node(node) for node in graph.nodes),
            ordered_node_ids=graph.ordered_node_ids,
            matched_node_ids=graph.matched_node_ids,
            as_of_seq=graph.as_of_seq,
            completeness=graph.completeness,
        )


class ConversationGraphDelta(_ResponseModel):
    """对话图增量，节点使用与历史快照相同的展示契约"""

    as_of_seq: int
    next_cursor: str | None
    turn_upserts: tuple[TraceGraphTurn, ...]
    turn_removes: tuple[str, ...]
    node_upserts: tuple[ConversationGraphNode, ...]
    node_removes: tuple[str, ...]
    ordered_node_ids: tuple[str, ...]
    matched_node_ids: tuple[str, ...]
    completeness: TraceGraphCompleteness

    @classmethod
    def from_graph(cls, graph: AgUiTraceGraphDelta) -> "ConversationGraphDelta":
        """转换已校验的框架图增量"""
        return cls(
            as_of_seq=graph.as_of_seq,
            next_cursor=graph.next_cursor,
            turn_upserts=graph.turn_upserts,
            turn_removes=graph.turn_removes,
            node_upserts=tuple(_conversation_node(node) for node in graph.node_upserts),
            node_removes=graph.node_removes,
            ordered_node_ids=graph.ordered_node_ids,
            matched_node_ids=graph.matched_node_ids,
            completeness=graph.completeness,
        )


class ConversationTraceUpdate(_ResponseModel):
    """只发送界面消费的增量与状态，同序号状态变化仍需应用"""

    generation: str
    observed_at: datetime
    as_of_seq: int
    has_events: bool = Field(description="本批是否包含已提交的事件或语义事实")
    messages: TraceEntityDelta[AgUiTraceMessage]
    reasoning: TraceEntityDelta[TraceReasoning]
    graph: ConversationGraphDelta
    interactions: TraceEntityDelta[AgUiTraceInteraction]
    state: TraceState
    status: TraceStatus
    completeness: TraceCompleteness
    message_count: int
    tool_call_count: int

    @classmethod
    def from_update(cls, update: AgUiTraceUpdate) -> "ConversationTraceUpdate":
        """业务投影消费完毕后生成对话流响应"""
        return cls(
            generation=update.generation,
            observed_at=update.observed_at,
            as_of_seq=update.as_of_seq,
            has_events=bool(update.events or update.facts),
            messages=update.messages,
            reasoning=update.reasoning,
            graph=ConversationGraphDelta.from_graph(update.graph),
            interactions=update.interactions,
            state=update.state,
            status=update.status,
            completeness=update.completeness,
            message_count=update.message_count,
            tool_call_count=update.tool_call_count,
        )


class ConversationGraphQueryPage(AgUiTraceGraphPage, frozen=True):
    """独立链路页及其查询身份，用于与并发到达的历史核对"""

    generation: str = Field(min_length=1)
    head_run_id: str = Field(min_length=1)

    @classmethod
    def from_page(
        cls, page: AgUiTraceGraphPage, *, generation: str, head_run_id: str
    ) -> "ConversationGraphQueryPage":
        """保留完整链路详情并附加此次授权查询使用的身份"""
        return cls(
            turns=page.turns,
            nodes=page.nodes,
            ordered_node_ids=page.ordered_node_ids,
            matched_node_ids=page.matched_node_ids,
            as_of_seq=page.as_of_seq,
            completeness=page.completeness,
            next_cursor=page.next_cursor,
            generation=generation,
            head_run_id=head_run_id,
        )
