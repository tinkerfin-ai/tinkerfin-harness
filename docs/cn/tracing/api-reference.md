# Tracing API 参考

[语义 Trace](index.md) · [English](../../en/tracing/api-reference.md)

## `Tracer`

```python
Tracer(
    store=None,
    capture_policy=None,
    reasoning_capture_policy=None,
    redactor=None,
    graph_query_limits=None,
    limits=None,
    write_policy=None,
    projections=(),
)
```

| 成员 | 含义 |
| --- | --- |
| `store` | 借用的 `TraceStore`；默认新建 `InMemoryTraceStore` |
| `capture_policy` | 正文保留、Tool 选择与错误消息策略 |
| `reasoning_capture_policy` | 独立控制是否保存已提取的 reasoning 正文 |
| `redactor` | 可选的额外业务 `TraceRedactor` |
| `graph_query_limits` | Graph 直接节点、补齐 Subagent 后的总节点与序列化字节上限 |
| `open_run(context)` | TinkerFin Runtime 使用的 `RuntimeObserver` 入口 |
| `get(identity, head_run_id=None, limit=100, history_cursor=None, projections=(), at_run_start=False)` | 固定前缀的会话历史 |
| `query(identity, where=None, head_run_id=None, cursor=None, limit=100)` | 当前索引上的 `TraceGraphQuery` |
| `rebuild_graph(identity)` | 从 Ledger fact 重建可丢弃的 Graph 索引 |

同时传入 `store` 与 `limits` 时，两者必须与 `store.limits` 完全一致。

查询接收 `tinkerfin_contracts.ThreadIdentity(namespace=..., thread_id=...)`，也可直接使用
`runtime.thread_identity(thread_id)`。允许传入 `RunIdentity`，但其中的 Run ID 不用于选择
历史分支；选择分支请传 `head_run_id`。
Store 可被多个 namespace 共用：`open_writer(identity)` 使用完整运行身份，
`snapshot(identity)` 使用完整会话身份。存储代、游标、检查点与删除均保留所属 namespace。
`max_tracer_threads` 和 `max_tracer_bytes` 分别限制每个 namespace。


`at_run_start=True` 读取所选运行开始输出之前的历史，保留已受理的用户输入和先前运行内容；不能与 `history_cursor` 同时使用。

## Graph 数据

`TraceGraphFilter` 只支持 `kinds`、`statuses`、`model_call_id`、`agent_names`、
`providers`、`models`、`graph_namespaces`、`search`、`started_after` 和 `started_before`。
`model_call_id` 筛选由同一次 Model 调用产生的 AssistantMessage、Tool 和 Subagent 事件；
`graph_namespaces` 按完整作用域精确匹配，空元组表示顶层 Graph。

`search` 按字面子串过滤事件名称、Agent 名称、provider、model 及已保留的公开详情，包括正文、
请求、结果和失败信息；JSON key、字符串值和标量值均可搜索。采集策略已省略的值、身份 ID 和
私有 reasoning 不参与搜索。只含 ASCII 的查询只折叠 ASCII `A-Z`；查询中包含非 ASCII 字符时
按大小写精确匹配。

正文搜索支持加密 Codec。元数据、Graph 作用域、Model 调用和时间筛选先缩小候选范围；
候选超过 `max_total_nodes` 时抛出 `TraceQuotaExceeded`，不会返回不完整的搜索结果。

`TraceGraphQuery` 提供：

- `snapshot`：完整 `TraceGraphPage` 的防御性副本；
- `turns`、`nodes`、`ordered_node_ids` 和 `matched_node_ids`；
- `next_cursor`、`as_of_seq` 与 `completeness`；
- `follow()`：只允许当前第一页使用的 `TraceFollow[TraceGraphDelta]`。

`TraceGraphNodeKind` 包含 `human_message`、`assistant_message`、`context`、`model`、
`tool`、`subagent`、`memory`、`guardrail`、`retrieval`、`custom`、`plan` 和
`interaction`。ToolMessage 结果由对应 Tool 事件持有。Middleware 执行没有专属 kind 或 fact；
读取 `SKILL.md` 只显示为普通 `read_file` Tool 事件。

`TraceGraphNodeStatus` 包含 `running`、`waiting`、`succeeded`、`failed`、`cancelled`、
`abandoned` 和 `unknown`。`TraceGraphLinkIssue` 包含 `missing_subagent`、
`missing_model_call` 和 `missing_tool_proposal`。

`TraceGraphTurn` 是容器。顶层事件彼此平级，只有 Subagent 拥有嵌套事件作用域。
`TraceGraphNode.parent_subagent_id` 表示最近的所属 Subagent，也是唯一的展示嵌套关系。非空
`graph_namespace` 不能证明这一关系，必须有经过校验的 Subagent 来源。
`TraceGraphNode.model_call_id` 只把 AssistantMessage、Tool 或 Subagent 与产生它的 Model 调用关联。
只有已观测的 AssistantMessage 没有用户可见正文，且对应 Model 结果确实发出至少一个 Tool 调用时，
`TraceGraphNode.tool_call_only` 才为 `true`；查询过滤掉 Tool 事件时，该值保持不变。

属于 Subagent 的 HumanMessage 通过 `content` 公开已采集的 `task.description`；对应 Subagent
通过 `request` 公开完整任务参数。

每次 Model attempt 都生成一个 `context` 节点。它的 `started_at` 是同一执行作用域中的上一项
可见边界，`completed_at` 与 Model 的 `started_at` 完全相同。`content` 从该 Model 请求中的最终
SystemMessage 内容投影；请求没有 SystemMessage
时，`content` 为 `None`，计时仍然有效。

同一作用域先按 `started_seq` 排序；序号相同时依次使用用户、上下文、模型、工具、子智能体、
助手和事件 ID。`ordered_node_ids` 中，Subagent 容器后紧接其内部事件。

`matched_node_ids` 只包含按事件顺序排列的直接匹配；`nodes` 还包含这些节点所属的 Subagent 容器，最多 64 层，
总量受 `max_total_nodes` 约束。未筛选历史 Graph 的全部返回事件均为直接匹配。

`TraceGraphDelta` 提供 Turn 和节点的 upsert/remove，以及当前 `next_cursor`、`as_of_seq`、完整
`ordered_node_ids`、`matched_node_ids` 和 Completeness。带分页 cursor 的查询不能 follow；当前
Ledger 尾序号变化后，旧 cursor 会明确失效，因此每个实时 Delta 都会原子替换上一 cursor。

`Tracer.get()` 返回的 `TraceThread.graph` 是与 messages、state 相同固定前缀及已加载 Turn
窗口对应的完整 `TraceGraph`。`TraceThread.follow()` 通过 `TraceUpdate.graph` 发布
`TraceGraphDelta`；历史与查询入口共用这套平级且仅按 Subagent 分域的时间线模型。当前尾部历史
读取可丢弃的 Graph 索引；固定旧前缀则从 Ledger fact 重放同一个 reducer。

`TraceGraphCompleteness` 分别表达调用历史无法确认、关系依据缺失和详情被采集或响应上限省略。
`call_tracking_missing` 不包含有明确 `runtime_initialization_error` 终态证据的执行前初始化失败；
其他未跟踪 Run 仍需报告，不能仅凭失败状态或没有调用事件判定历史完整。

## 查询上限

```python
TraceGraphQueryLimits(
    max_direct_nodes=1000,
    max_total_nodes=4000,
    max_page_bytes=8 * 1024 * 1024,
)
```

直接匹配先受节点上限约束，再补齐所属 Subagent；`max_total_nodes` 同时限制补齐后的完整页面和
精确正文搜索需要解码的候选节点。一次查询最多选择 10,000 个 lineage Run。响应超过字节预算
时，框架先省略 content、request、result、usage 与响应元数据，同时保留权威结构；仅结构仍
超限时抛出 `TraceQuotaExceeded`。

## Capture 策略

| API | 用途 |
| --- | --- |
| `CapturePolicy.public_history(...)` | 默认保存完整且已脱敏的 Tool 正文 |
| `CapturePolicy.public_safe(...)` | Tool 默认只保留元数据，除非显式选择路径 |
| `ToolTraceCapture.full_content()` | 保存 Tool 生命周期、参数、结果和公开审批说明 |
| `ToolTraceCapture.metadata_only()` | 保存生命周期但不保存正文 |
| `ToolTraceCapture.selected_content(...)` | 保存选定的 RFC 6901 路径 |
| `ToolTraceCapture.disabled()` | 不生成该 Tool 的 Trace fact |
| `ReasoningCapturePolicy.omitted()` | 不保存已提取 reasoning 的正文或 digest |
| `ReasoningCapturePolicy.content()` | 授权保存有界的已提取 reasoning 正文 |

`CapturePolicy` 不提供直接处理原始值的方法。Runtime 值统一进入 `Tracer` 拥有的强制管道。

## 业务脱敏

```python
class TraceRedactor(Protocol):
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue: ...
```

`RedactionContext.content_kind` 可取 `message`、`model_request`、`model_response`、
`tool_arguments`、`tool_result`、`state`、`interaction`、`plan`
或 `custom`。`component_name` 只包含可选的公开组件名，不提供 thread、Run 或用户身份。

`CompositeRedactor(*redactors)` 按声明顺序传递结果。
`redact_json_paths(value, paths=(...))` 返回独立副本，并把匹配的 RFC 6901 位置替换为
`{"$type": "redacted"}`。

Redactor 必须同步、确定、可重入、无 I/O，且不得修改输入。异常、awaitable、原地修改、非法
JSON、非有限数字或破坏 state、模型消息及 HITL 必需结构时，框架抛出
`TraceCaptureRejected`，不会回退保存原文。

框架在业务链前后都执行凭据与已验证私有 reasoning 清理，最终安全检查不能关闭。

## 存储接口

`RunFact` 表示整个 Runtime 调用，`graph_namespace` 必须为空，`in_subagent_scope` 必须为 false；
子 Agent 执行使用 `SubagentFact`。构造时会拒绝其他作用域。Writer 也会在提交前原子拒绝含有
非法 Run 作用域的整批事实，包括通过不执行校验的模型复制方式构造的输入。

`MessageFact.phase` 包含 `started`、`content`、`completed`、`reconciled`、`removed`、
`cancelled`、`interrupted` 和 `abandoned`。最后三项表示 Run 结算时仍未完整交付的助手消息，
可以携带已保留的部分正文，不能表示状态快照或其他消息角色。`TraceMessage.status` 在交付结束
或暂停时使用 `completed`；Graph 单独表达成功、取消、等待或放弃。恢复后的消息可以重新进入
`streaming`，且不会重复已有正文。

`SubagentFact` 的阶段与状态对应为 `started/running`、`updated/waiting`；`completed`
只接受 `succeeded`、`failed`、`cancelled` 或 `abandoned`。`input`、`parent_tool_call_id`、
`parent_execution_id` 和 `model_call_id` 只在 `started` 保存。后续事实沿用相同的
`subagent_id` 与 graph_namespace，从开场事实取得请求和关系。

| 接口 | 职责 |
| --- | --- |
| `TraceStore` | Ledger generation、固定读取、follow、checkpoint 与删除 |
| `TraceGraphStore` | 有界的直接 Graph 查询 |
| `TraceGraphRebuildStore` | 重建可丢弃的 Graph 索引 |
| `TraceLedgerBackend` | 五操作持久 Ledger 边界 |
| `TraceGraphQueryBackend` | 可选的持久 Graph 索引查询 |
| `TraceGraphRebuildBackend` | 可选的持久 Graph 重建 |
| `CanonicalTracePayloadCodec` | 规范编码或可逆加密转换 |

Graph Store 扩展通过只读属性 `supports_graph_queries`、`supports_graph_rebuild`
声明实际能力，`DurableTraceStore` 根据后端提供的功能返回对应值。仅支持 Ledger 的
后端仍可读取普通历史；显式请求未支持的索引查询或重建会抛出 `TraceStoreProtocolError`。

低层 writer 只持久化已经 Capture 的 fact。需要框架和业务强制脱敏时，应使用能够获得来源
上下文的 `Tracer` 路径。

`TraceGraphNodeMutation` 建立 Model 关联时必须同时提供 `model_call_id` 和 `model_call_seq`。
`StoredTraceGraphNode` 与 `TraceGraphNodeRecord` 用该序号及 `model_call_event` 单独保留关联
依据，不随生命周期或正文引用更新而丢失。该事实必须证明同一节点在所选作用域与 Run 分支中的
关联；无关的 Model 事实或后续入参更新不能充当关系依据。
