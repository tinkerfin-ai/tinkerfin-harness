# 审批与恢复

[AG-UI 事件](events.md) · [English](../../en/agui/interrupts-and-resume.md)

Agent 可以在执行工具前暂停，等待用户决定，然后继续同一 namespace 和 thread 的运行。

## 配置工具审批

以下示例使用业务已定义的 `delete_order` 工具：

```python
from langgraph.checkpoint.memory import InMemorySaver
from tinkerfin import TinkerFin
from tinkerfin.coordination import InMemoryRunCoordinator

runtime = (
    TinkerFin(
        checkpointer=InMemorySaver(),
        run_coordinator=InMemoryRunCoordinator(),
    )
    .with_namespace("company-a")
    .build(
        model="openai:gpt-5.4",
        tools=[delete_order],
        interrupt_on={"delete_order": {"allowed_decisions": ["approve", "reject"]}},
    )
)
```

恢复需要 checkpointer。内存存储和协调器适合单进程；跨进程部署应配置共享持久化存储和协调器。

## 提交决定

每项决定对应终止事件中的一个 interrupt，请求必须覆盖该批次全部待处理项：

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"type": "approve"}
}
```

| 字段 | 用途 |
| --- | --- |
| `interruptId` | 服务端发布的 interrupt ID |
| `status` | `resolved` 表示提交决定，`cancelled` 表示放弃 |
| `payload` | interrupt 响应 Schema 允许的决定；放弃时不携带 |

应用负责认证和审批权限。客户端只提交决定，不能提供用于授权的原 interrupt 内容、工具关联或 checkpoint 信息。

## 继续执行

```python
from contextlib import aclosing
from tinkerfin import AgUiResumeRequest

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="approval-1",
        resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    )
) as events:
    async for event in events:
        await send_event(event)
```

框架校验待审批内容、工具关联和决定范围。未知 ID、覆盖不完整、过期来源、非法参数和不允许的决定会在继续执行前失败。

| 决定组合 | 行为 |
| --- | --- |
| 全部 `resolved` | 保存请求并继续执行 |
| 全部 `cancelled` | 放弃本次请求，返回取消终态，不执行 Graph |
| 工具批次混合两者 | 执行已决定的工具，为放弃的工具返回未执行结果 |

放弃不会转换成拒绝。混合工具审批要求对应工具具备 TinkerFin 的审批支持；Plan 与工具审批不能合为一个批次。

## 重试、并发与回调

- 每个新审批请求使用新的 `run_id`；重试原请求时保留相同的 `run_id`、`thread_id` 和决定。
- 对同一 namespace 和 thread 的写入串行执行。使用共享协调器，或由宿主保证这一点；同时覆盖 Native 和 AG-UI 调用。
- 重试使用原审批批次。子 Agent 进入下一轮审批后，旧请求不会改为批准新问题。
- 并行执行中已完成的任务保持完成；恢复只补交尚未消费的原决定。
- 同一 Graph 节点中、interrupt 之前的外部副作用仍需幂等。checkpoint 不会把外部写入变成事务。

需要结算业务审批时，可传入两个异步回调：

| 参数 | 调用条件 |
| --- | --- |
| `on_resume_saved` | 请求已经持久保存；接收 `AgUiResumeCheckpoint`，重试会再次交付同一记录 |
| `on_resume_not_saved` | 请求未保存就失败或取消；用于释放业务认领 |

两个回调都应幂等。`on_resume_saved` 表示请求已保存，不表示工具执行成功；不要根据 `RUN_STARTED` 结算审批。

自定义 Graph 在同一 checkpoint/task 中连续调用 interrupt 时，AG-UI 不接受新一轮审批；应让每轮审批经过新的 Graph 步骤。存在其他 Native 调用遗留的全局恢复值时，框架会拒绝继续，防止该值回答未经批准的问题。

## Plan 澄清与审批

使用 `.with_plan(enabled=True)` 构建 Runtime，并在运行时选择 `mode="plan"`。恢复仍使用 `open_agui_run(resume=...)`：

| `reason` | 回复要求 |
| --- | --- |
| `tinkerfin:plan_clarification` | `type="respond"`，`answers` 完整覆盖问题 ID；遵循每道题的响应 Schema |
| `tinkerfin:plan_review` | 使用允许的动作，并携带当前 `baseRevision` |

Plan interrupt 没有 `toolCallId`。客户端不能回传 Form、label 或其他可信表单内容；框架从 checkpoint 恢复它们。未知选项、跳过必填题和过期 `baseRevision` 都会失败。放弃 Plan 后，后续普通输入可以选择 `mode="default"`。

## 单独使用 Adapter

自行管理完整可信事件日志的集成，可以调用 `AgUiResumeBinding.from_agui(entries=..., interrupts=...)`。拥有原生 checkpoint 的集成，可以使用 `ResumeMapper.map()` 并提供完整的工具消息与 Graph 位置。普通 Runtime 使用者无需翻译原生 Command。

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)
