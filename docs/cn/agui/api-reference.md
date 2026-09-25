# AG-UI API

[AG-UI](index.md) · [English](../../en/agui/api-reference.md)

## Runtime API

| API | 用途 |
| --- | --- |
| `runtime.open_agui_run(...)` | 返回惰性、单次消费的 `AgUiRunStream` |
| `AgUiRunStream.abort()` | 请求取消并返回剩余终态事件 |
| `AgUiRunStream.aclose()` | 等待执行与清理完成 |
| `AgUiRunStream.to_sse()` | 返回单次消费的 UTF-8 SSE 字节流 `SseBody[bytes]` |
| `AgUiResumeRequest` | 携带 pending interrupt 的不可信客户端决定 |
| `AgUiResumeReceipt` | 已保存请求的不可变回执，包含 `identity`、`parent_run_id`、不透明的 `receipt_id` 和 `responses` |
| `AgUiResumeResponse` | 一条已保存回复的公开 `interrupt_id` 和 `status`，不含决定载荷 |

### `open_agui_run()`

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `thread_id`、`run_id` | 必填 | Runtime namespace 内的运行身份 |
| `messages` / `input` / `resume` | 严格三选一 | 用户消息、高级原生状态或恢复决定 |
| `parent_run_id` | `None` | 已授权的分支或恢复来源 |
| `mode` | Runtime 默认值 | `default` 或已配置的 `plan` |
| `config`、`context` | `None` | Graph 设置与带类型调用上下文 |
| `stream_timeout` | `None` | 原生流时限 |
| `cleanup_timeout` | `None` | 调用方等待受保护清理的时限 |
| `include_reasoning_events` | `False` | 输出已验证公开推理事件 |
| `include_subagent_events` | `True` | 输出子智能体事件 |
| `on_native_part`、`on_agui_event` | `None` | 交付前的异步观察函数 |
| `on_resume_saved`、`on_resume_not_saved` | `None` | 仅恢复分支使用的结算回调 |

`AgUiSettlementTimeoutError` 表示清理仍由流持有。释放共享资源前，应再次等待 `aclose()`。

## 输入辅助类型

宿主在授权后才分配消息 ID 时，可用 `AgUiUserInput` 校验一条无 ID 用户消息。
`with_attachments(AttachmentSupport(read_content=...))` 配置模型调用前按附件 ID 进行的授权读取。
原生媒体不需要读取函数，框架默认按目标模型声明的输入能力检查。

`AgUiResumeBinding` 是框架解析后保存的恢复值。普通宿主把 `AgUiResumeRequest` 交给 Runtime，不自行构造 Binding。

## 独立 Adapter

`tinkerfin-agui-adapter` 可以转换已有原生流，不创建 `AgentRuntime`：

| API | 用途 |
| --- | --- |
| `astream_events(parts, identity=...)` | 为原生流管理完整 AG-UI 生命周期 |
| `DeepAgentAgUiAdapter` | 自定义编排器拥有主生命周期时逐条转换 |
| `encode_sse(event, event_id=...)` | 把一条事件编码为 SSE 文本（`str`） |
| `micro_batch(events)` | 在不跨生命周期边界的前提下合并相邻小增量 |
| `ScopedIdCodec` | 使用完整 Graph 位置编码和解码 ID |
| `ResumeMapper` | 转换可信原生或已保存 AG-UI interrupt 证据 |

Adapter 的 `RunIdentity` 只包含 AG-UI thread 和 run ID。业务 namespace 由上层 Runtime 或宿主选择。

## Interrupt 契约

| 模型 | 用途 |
| --- | --- |
| `AgentRuntimeInterrupt` | 稳定 interrupt ID 与 JSON 值 |
| `RuntimeInterruptEnvelope` | 已校验的非工具工作流暂停 |
| `HitlRequest` | 配对的工具动作与审阅策略 |
| `ToolReviewInterruptMetadata` | 稳定工具审阅关联数据 |
| `SubagentProvenance` | 稳定子智能体调用与完整 Graph 位置 |

公开工具决定包括 `approve`、`edit`、`reject` 和 `respond`。编辑后的参数在转换为原生 resume 前按动作 Schema 校验。完整流程见 [Interrupt 与恢复](interrupts-and-resume.md)。
