# 看懂 AG-UI 事件

[AG-UI 入门](index.md) · [English](../../en/agui/events.md)

一次正常运行从 `RUN_STARTED` 开始，以一个 `RUN_FINISHED` 或 `RUN_ERROR` 结束。中间事件按实际运行顺序到达。

## 最常见的事件

| 事件 | 前端通常怎么用 |
| --- | --- |
| `RUN_STARTED` | 建立运行状态；Graph 输入已经显式提供，因此 `input` 缺省 |
| `TEXT_MESSAGE_START` | 创建一条新的 Agent 消息 |
| `TEXT_MESSAGE_CONTENT` | 追加回答文字 |
| `TEXT_MESSAGE_END` | 结束这条消息 |
| `TOOL_CALL_START` | 显示正在调用哪个工具 |
| `TOOL_CALL_ARGS` | 逐步追加工具参数 |
| `TOOL_CALL_END` | 工具参数已经完整 |
| `TOOL_CALL_RESULT` | 显示工具执行结果 |
| `STATE_SNAPSHOT` / `STATE_DELTA` | 更新前端状态 |
| `MESSAGES_SNAPSHOT` | 用完整消息快照校准历史 |
| `RUN_FINISHED` | 标记成功或等待用户处理 interrupt |
| `RUN_ERROR` | 标记运行失败 |

同一条消息或工具调用可能分成很多 delta。前端要按事件中的完整 ID 归并，不能按“谁先返回”猜测对应关系。

## 一个简单顺序

没有工具调用时，通常能看到：

```text
RUN_STARTED
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT ...
TEXT_MESSAGE_END
STATE_SNAPSHOT / STATE_DELTA
RUN_FINISHED
```

有工具调用时，文字和工具事件可能交错；并行工具也可能同时处于开放状态。只要按 ID 更新各自的 UI 即可。

工具参数片段不会结束同一模型消息的文字输出。即使工具片段之后还有文字，这条消息也只开始
和结束一次。工具事件中的父消息 ID 与消息快照中的身份保持一致。

## 主 Agent 和子 Agent

子 Agent 来自非根 graph_namespace。转换器会保留完整 graph_namespace 和来源信息，避免不同子 Agent 的消息、工具和状态混在一起。

`expose_subagent_events=True` 会发送子 Agent 的公开事件。设置为 `False` 时，转换器仍会检查这些数据，但不把对应事件交给前端。

框架不会用 `parentRunId` 表示 LangGraph 子图。高层 Runtime 显式接收 `parent_run_id`，并且只把它
用于 checkpoint 分支。经过校验的 Deep Agents
委派会在 task RAW descriptor 中携带 `tinkerfin.subagent-provenance`：
`subagentInvocationId` 跨 resume 稳定，`requestRunId` 表示当前主请求，子事件 source 重复该
invocation ID，父 task Result 使用 `relatedSubagentInvocationId`。

## 推理事件

如果界面需要显示支持的推理过程：

```python
from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = TinkerFin().with_namespace(namespace).build(model=model)
async with aclosing(
    runtime.open_agui_run(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
        include_reasoning_events=True,
    )
) as events:
    async for event in events:
        await send_event(event)
```

你可能收到 `REASONING_START`、`REASONING_MESSAGE_*` 和 `REASONING_END`。不是所有模型都会产生可公开的推理事件，也不能假设内容为空的模型 chunk 就是心跳。

无论开关是否启用，provider 私有推理元数据都不会作为普通状态、消息或 raw payload 直接公开。

## `messages`、`tasks`、`values` 分别做什么

| 原生模式 | 转换时提供的信息 |
| --- | --- |
| `messages` | 文本 chunk、工具参数 chunk、工具结果及消息 metadata |
| `tasks` | Graph 节点和任务的开始、结果、错误及 graph_namespace 关系 |
| `values` | 每一步之后的状态快照，以及顶层 `interrupts` |

`tasks` 是复数；它不等于 Deep Agents 中名为 `task` 的子 Agent 工具。根 Graph 与子图的 `values` 也是不同状态范围，前端或服务端不能用后到的子图状态覆盖根状态。

## 终止事件

每次主运行只应有一个终止事件：

- 成功完成：`RUN_FINISHED`，outcome 为 success；
- 等待审批：`RUN_FINISHED`，outcome 带 interrupts；
- 运行失败：`RUN_ERROR`。

文本、推理和工具生命周期会在主终止事件之前关闭。客户端断开时，应取消或关闭服务端流，不要自行伪造成功事件。

## 观察或转发事件

```python
async def audit_event(event) -> None:
    await audit_log.write(event.model_dump(mode="json", by_alias=True))


from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = TinkerFin().with_namespace(namespace).build(model=model)
async with aclosing(
    runtime.open_agui_run(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
        on_agui_event=audit_event,
    )
) as events:
    async for event in events:
        await send_event(event)
```

`on_agui_event` 在事件交给消费者前执行。它适合审计和指标，不适合阻塞 I/O。

下一篇：[interrupt 与恢复](interrupts-and-resume.md)。
