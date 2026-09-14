# 语义 Trace

[API 参考](api-reference.md) · [English](../../en/tracing/index.md)

TinkerFin Tracing 记录运行、消息、模型调用、工具、审批与子智能体，可查询会话历史、查看执行图，
也可订阅实时变化。

## 记录会话

```python
from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer

tracer = Tracer()
runtime = (
    TinkerFin()
    .with_observer(tracer)
    .with_namespace("company-a")
    .build(model="openai:gpt-5.4")
)
result = await runtime.ainvoke(
    thread_id="conversation-1",
    run_id="request-1",
    input={"messages": [{"role": "user", "content": "你好"}]},
)
thread = runtime.thread_identity("conversation-1")
history = await tracer.get(thread)
print(history.messages, history.summary.status.execution)
```

一个 Tracer 和 Store 可以服务多个 namespace。使用者决定 Runtime 的 namespace 并负责访问授权；
不同 namespace 下相同的 thread 和 Run ID 拥有独立历史与写入归属。没有 Runtime 时，可使用
`tinkerfin_contracts.ThreadIdentity(namespace=..., thread_id=...)` 查询。

调用和流式执行都会记录输入及模型输出前的初始化失败。流必须消费完毕或显式关闭；直接执行
编译后的 Graph 不会自动记录完整运行。

## 历史与执行图

`tracer.get(thread)` 返回消息、reasoning、状态、审批、运行摘要与 `history.graph` 的固定视图。
存在多个分支时使用 `head_run_id` 选择分支。`history.load_older()` 扩展同一固定视图，
`history.events()` 分页读取已保留事实。历史游标始终绑定原 namespace、存储代与事件前缀。

```python
from tinkerfin_tracing import TraceGraphFilter, TraceGraphNodeKind

graph = await tracer.query(
    thread,
    where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL, TraceGraphNodeKind.TOOL}),
)
print(graph.nodes, graph.matched_node_ids)
```

每个 Turn 表示一次用户任务。只有子智能体包含嵌套事件；模型关联用于连接相关消息与工具，
不改变展示层级。工具保留提议、实际执行、结果及审批结论。关系依据缺失、调用历史不完整和
正文省略均有明确标记；取消或中断的响应只保留实际采集到的内容。

可按类型、状态、模型、供应商、智能体、图内作用域、时间与已保留公开正文筛选。
搜索使用字面子串：纯 ASCII 查询忽略 ASCII 字母大小写，含非 ASCII 字符时精确匹配。
查询上限分别约束直接匹配、补齐的子智能体、搜索正文与响应字节数；子智能体最多嵌套 64 层。
结构或搜索容量超限时抛出 `TraceQuotaExceeded`。

## 实时更新

`history.follow()` 订阅历史和执行图变化；`graph.follow()` 仅用于当前第一页。
图游标绑定当前历史末尾及筛选条件，新提交会使其失效。使用异步上下文，确保提前退出循环也会关闭订阅：

```python
async with history.follow() as updates:
    async for update in updates:
        await handle(update)
```

其他 Store 实例提交的变化默认每 0.5 秒检查一次，可通过
`TraceStoreOptions.follow_poll_seconds` 调整。Writer 关闭或租约到期，但缺少已记录终态时，
运行显示 `unknown` 与 `missing_tail=True`，不能据此判断 Agent 成功。有效接管可以在相同
事件序号恢复为 `running`。

同一存储代内按 `(as_of_seq, observed_at)` 比较更新，并保留 UTC 时间精度；相同观测内容
冲突时需重新读取。拉取期间取消、拉取异常或退出异步上下文时，会关闭上游订阅。

## 采集与脱敏

`CapturePolicy.public_history()` 保留已脱敏的公开内容，包括工具输入与结果；`public_safe()`
仅采集工具元数据。单个工具可通过 `ToolTraceCapture` 选择策略。

凭据脱敏与已确认的供应商私有 reasoning 清理始终生效。业务脱敏函数接收独立 JSON 和
`RedactionContext`，必须同步、确定、无 I/O 且不修改输入；非法结果会拒绝采集。
供应商 reasoning 需要 Runtime 启用提取器，并由 Tracer 明确设置
`ReasoningCapturePolicy.content()`；默认不保留。

## 存储

`InMemoryTraceStore` 仅在当前进程有效。`SqlAlchemyTraceStore(engine)` 接收借用的异步
PostgreSQL、MySQL 或 SQLite Engine。安装 `"tinkerfin-tracing[sqlalchemy]"` 及所选异步驱动；
接收请求前调用 `setup()` 创建空库结构或校验既有表。

关闭 writer 并等待已接受操作结束后才能释放 Engine。内存 SQLite 使用
`AsyncAdaptedQueuePool`，设置 `pool_size=1, max_overflow=0`，不接受 `StaticPool`。
租约使用数据库 UTC 时间；MySQL 图查询需要 MySQL 8 或更新版本。
`max_tracer_threads` 和 `max_tracer_bytes` 分别限制每个 namespace。

可用 `await tracer.rebuild_graph(thread)` 重建
图索引，不改写已记录事件。Codec 可以加密事件与检查点；直接调用 writer 时应传入已采集
事实，需要框架及业务脱敏时使用 Tracer。

自定义存储实现 `TraceLedgerBackend`，图查询和重建使用独立的可选协议。
`verify_trace_ledger_backend()` 检查共享记录行为；同时定制查询与持久化时替换 `TraceStore`。
Messaging 投递和回放使用各自的存储契约。
