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
| `on_resume_saved` | 已校验的请求持久保存后、继续执行前调用；接收 `AgUiResumeReceipt` |
| `on_resume_not_saved` | 请求未保存就失败或取消；用于释放业务认领 |

两个回调都应幂等。`on_resume_saved` 表示请求已保存，不表示工具执行成功；不要根据 `RUN_STARTED` 结算审批。

不可变回执包含已绑定的 `identity`、实际使用的 `parent_run_id`、不透明的 `receipt_id`，以及按公开 `interrupt_id` 排序的 `AgUiResumeResponse` 元组。每条回复只有 `interrupt_id` 和 `status`，不含决定载荷。等价请求的重试会交付相等的回执，包括回复顺序不同的请求。整批取消走放弃路径，不调用 `on_resume_saved`。

宿主负责审批存储和权限。业务结算直接消费回执，以 `receipt_id` 去重，从 `responses` 读取已保存的审批结果：

```python
from tinkerfin import AgUiResumeReceipt

async def record_saved(receipt: AgUiResumeReceipt) -> None:
    await approval_repository.settle_resume(receipt=receipt)

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="approval-1",
        resume=AgUiResumeRequest(entries=tuple(resume_entries)),
        on_resume_saved=record_saved,
    )
) as events:
    async for event in events:
        await send_event(event)
```

框架为提交的请求建立快照，完成校验和持久保存后等待回调，再继续执行。回调无需保留客户端回复或重建原生决定。回调失败时，使用同一请求重试；已保存的回执仍然有效，继续执行会等待业务结算成功。

自定义 Graph 在同一 checkpoint/task 中连续调用 interrupt 时，AG-UI 不接受新一轮审批；应让每轮审批经过新的 Graph 步骤。存在其他 Native 调用遗留的全局恢复值时，框架会拒绝继续，防止该值回答未经批准的问题。

## Plan 澄清与审批

使用 `.with_plan(enabled=True)` 构建 Runtime，并在运行时选择 `mode="plan"`。恢复仍使用 `open_agui_run(resume=...)`：

| `reason` | 回复要求 |
| --- | --- |
| `tinkerfin:plan_clarification` | 提交答案使用 `type="respond"`，`answers` 完整覆盖问题 ID；转为讨论使用 `type="discuss"` 和非空 `message`，不携带 `answers` |
| `tinkerfin:plan_review` | 使用允许的动作，并携带当前 `baseRevision`；允许 `respond` 时，可用该动作和非空 `message` 转为讨论 |

Plan interrupt 没有 `toolCallId`。客户端不能回传 Form、label 或其他可信表单内容；框架从 checkpoint 恢复它们。未知选项、跳过必填题和过期 `baseRevision` 都会失败。放弃 Plan 后，后续普通输入可以选择 `mode="default"`。

讨论请求会结束原卡片的等待状态，旧卡片不能再次提交或批准。框架保留原表单或草稿作为对话上下文，不把未提交的表单内容当作答案。模型随后可以普通回复、提出新澄清或生成新草稿；新草稿仍需批准。

普通回复由规划模型直接生成，以普通消息流输出并保存在历史中；不需要额外的行为选择调用。本轮结束后等待新的用户消息，不自动开始下一轮规划。

只结束卡片而不发送讨论时，澄清使用 `type="dismiss"`，草稿使用 `type="dismiss"` 和当前 `baseRevision`。关闭不提交未完成答案、不调用模型、不授权执行；关闭后继续保持 Plan 模式，等待下一条用户消息。原表单或草稿保留为可信上下文。

例如，对澄清卡片发起讨论仍只需要一次恢复调用；`runtime` 是已配置 Plan 的 Runtime，`interrupt_id` 来自当前服务端卡片：

```python
from ag_ui.core.types import ResumeEntry

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="discussion-1",
        mode="plan",
        resume=AgUiResumeRequest(entries=(ResumeEntry(
            interrupt_id=interrupt_id,
            status="resolved",
            payload={"type": "discuss", "message": "第三题是什么意思？"},
        ),)),
    )
) as events:
    async for event in events:
        await send_event(event)
```

宿主仍负责认证和传输。卡片校验、讨论消息身份、上下文保存、恢复去重和资源关闭由框架处理；不需要先取消卡片再发起第二次运行。

## 单独使用 Adapter

自行管理完整可信事件日志的集成，可以调用 `AgUiResumeBinding.from_agui(entries=..., interrupts=...)`。拥有原生 checkpoint 的集成，可以使用 `ResumeMapper.map()` 并提供完整的工具消息与 Graph 位置。普通 Runtime 使用者无需翻译原生 Command。

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)
