"""对话展示与独立链路查询的 HTTP 数据边界"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from tinkerfin.agui import (
    AgUiSubagentReference,
    AgUiToolReference,
    AgUiTraceGraph,
    AgUiTraceGraphNode,
    AgUiTraceGraphPage,
)
from tinkerfin_tracing import (
    TraceGraphCompleteness,
    TraceGraphFailure,
    TraceGraphLinkIssue,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphTurn,
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
    request_reference: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="按需读取模型请求正文的会话内引用",
    )
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
