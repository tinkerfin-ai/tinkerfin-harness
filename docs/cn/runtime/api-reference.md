# Runtime API

[Runtime](index.md) · [English](../../en/runtime/api-reference.md)

AG-UI 入口需要安装 `tinkerfin[agui]`。

## Builder

| API | 用途 |
| --- | --- |
| `TinkerFin(checkpointer=..., run_coordinator=..., store=..., runtime_profile=...)` | 配置共享的借用资源 |
| `.with_namespace(namespace)` | 选择应用隔离范围；调用 `build()` 前必须设置 |
| `.with_observer(observer)` | 添加 Runtime observer |
| `.with_observer(on_terminal=callback)` | 添加一个异步终态回调 |
| `.with_plan(...)` | 启用 Plan，并选择模型、表单、内容和审阅动作 |
| `.with_attachments(support)` | 启用由宿主授权的附件解析 |
| `.build(model, tools, ...)` | 保存智能体配置并返回 `AgentRuntime` |

所有配置方法都返回独立 builder。`build()` 不执行 I/O，也不接管传入资源的所有权。

### 主要构建参数

| 参数 | 用途 |
| --- | --- |
| `model`、`system_prompt` | 模型与指令 |
| `tools`、`middleware` | 智能体工具与 middleware |
| `subagents` | 声明式、compiled 或 remote subagent |
| `skills`、`memory` | 技能目录与记忆文件 |
| `backend`、`permissions` | 文件 backend 或惰性 workspace 及文件规则 |
| `interrupt_on` | 指定执行前需要审批的工具 |
| `response_format` | 结构化输出契约 |
| `state_schema`、`context_schema` | 持久状态字段与调用上下文类型 |

`model` 必填；持久化通过 `TinkerFin(checkpointer=..., store=...)` 配置。
`context_schema` 决定各执行方法的 `context` 参数类型。

## AgentRuntime

| API | 结果 |
| --- | --- |
| `runtime.namespace` | 构建 Runtime 时固定的 namespace |
| `runtime.thread_identity(thread_id)` | 完整 namespaced thread 身份 |
| `runtime.run_identity(thread_id, run_id)` | 完整 namespaced run 身份 |
| `await runtime.ainvoke(...)` | 最终状态的防御性副本 |
| `runtime.open_run(...)` | 惰性 `NativeRunStream` |
| `runtime.open_agui_run(...)` | 惰性 `AgUiRunStream` |
| `runtime.agui.history(tracer)` | 读取当前 Runtime namespace 下的 AG-UI 对话历史 |

所有执行方法都接收 `thread_id`、`run_id`、普通或 AG-UI 输入，以及可选的
`mode`、Graph `config`、带类型 `context`、观察回调和受支持的 LangGraph 流控制参数。

`open_agui_run()` 必须且只能提供以下一种输入：

| 输入 | 用途 |
| --- | --- |
| `messages` | 带最终唯一 ID 的标准 AG-UI 用户消息 |
| `input` | 高级原生智能体状态 |
| `resume` | 覆盖全部 pending interrupt 的 `AgUiResumeRequest` |

`on_resume_saved` 和 `on_resume_not_saved` 只用于恢复分支。它们围绕已持久化的 resume marker 结算宿主状态，框架会等待其完成。

## 读取 AG-UI 对话历史

安装 `tinkerfin[agui,tracing]`，选择已有 Tracer 作为历史来源：

```python
from tinkerfin.agui import AgUiHistory

history = AgUiHistory(tracer, namespace=namespace)
view = await history.get(thread_id)
snapshot = view.snapshot

async with view.follow() as updates:
    async for update in updates:
        await publish(update)
```

已有 Runtime 时，可用 `runtime.agui.history(tracer)` 读取其 namespace 下的历史。
这两个入口都不创建智能体，也不接管 Tracer 或 Store；应用需先确认访问该 namespace
和会话的权限。

快照和更新包含消息、工具、子智能体及待处理交互对应的实时 AG-UI 引用。
引用缺失或交互内容未完整留存时，`agui=None`，不能据此构造审批请求。
通过 `view.trace` 可读取原始事实和已注册的业务投影。

`await view.load_older(limit=100)` 在同一历史范围内加载更早内容。筛选执行图时，
使用 `await history.query(thread_id, where=filters)`，读取其 `snapshot` 或
`follow()` 更新。图分页游标要求筛选条件和图末尾不变；图变化后需重新查询。
可能提前结束跟随时使用 `async with`；取消和查询异常会继续传播，关闭跟随不会关闭
调用方的历史来源。

刷新页面或新设备接续正在运行的会话时，使用 `open_live()`：

```python
channel = messaging.agui_channel(name="conversations")
live = await history.open_live(thread_id, channel=channel, head_run_id=run_id)
try:
    await send_snapshot(live.history.snapshot, replay=live.body is not None)
    if live.body is not None:
        async for frame in live.body:
            await send_bytes(frame)
finally:
    await live.aclose()
```

`send_snapshot` 与 `send_bytes` 由宿主传输层实现。运行中返回输出前的历史基线，
随后重播已提交增量并继续接收实时事件；已结束时返回完整历史且 `body=None`。
同页断网且完整视图仍在时，可传 `last_event_id` 并只消费后续字节；浏览器刷新时应省略。
历史帧带 SSE `event: replay`，表示此前已提交的事件；后续事件使用 `message`。
历史游标与消息 SSE ID 属于不同记录，不能混用。关闭 `live` 只停止读取，不取消后台运行；
消息已过期或无法读取时显式报错，不重新执行智能体。

## 流

| API | 用途 |
| --- | --- |
| `NativeRunStream` | 迭代已校验原生对象；`to_sse()` 使用规范化回放值 |
| `AgUiRunStream` | 迭代 AG-UI 事件；`abort()` 返回剩余取消事件 |
| `stream.aclose()` | 完成本次运行清理；清理超时后可再次等待 |
| `stream.to_sse()` | 返回单次消费的 UTF-8 SSE 字节流 `SseBody[bytes]` |
| `SseBody.prepare(preflight=...)` | 响应开始前执行检查 |

`AgUiSettlementTimeoutError` 表示清理仍由流持有。关闭共享资源前，应再次等待 `aclose()`。

`NativeStreamPart` 是有限原生回放值。Python 字段 `graph_namespace` 表示 Graph
位置，第三方 wire alias 仍为 `ns`。业务隔离始终使用 `RunIdentity.namespace`。

## 公共模块

常用 Runtime 类型、流、SSE helper 和异常从 `tinkerfin` 根入口导出。扩展契约位于对应模块：

| 模块 | API 范围 |
| --- | --- |
| `tinkerfin.agui` | `AgUiHistory` 与对话历史模型 |
| `tinkerfin.tools` | 提供业务上下文、运行身份与工作区的 `ToolRuntime` |
| `tinkerfin.media` | `Attachment`、`AttachmentContent` 和 `AttachmentSupport` |
| `tinkerfin.subagents` | 声明式 `SubAgent` 配置 |
| `tinkerfin.runtime_profile` | Deep Agents Runtime Profile |
| `tinkerfin.native_driver` | 原生流 Driver 与 reasoning extractor |
| `tinkerfin.coordination` | 运行协调协议与内存实现 |
| `tinkerfin.redis` | Redis 协调器、租约与租约异常 |
| `tinkerfin.plan` | Plan 表单、内容模型、决策与异常 |
| `tinkerfin.deep_agent` | 由调用方管理的 Graph 构建入口 |

模型、checkpointer、Store、数据库连接池和服务 client 均由应用打开和关闭。受管运行负责关闭自己创建的 Graph、workspace、流和每次运行资源。
